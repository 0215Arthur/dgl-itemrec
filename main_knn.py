import dgl
import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
import numpy as np
import pandas as pd
import scipy.stats
import scipy.sparse as ssp
import tqdm
import pickle
import argparse
from sklearn.metrics.pairwise import cosine_similarity
import sh
from model.pinsage import PinSage
from model.ranking import evaluate
from model.movielens2 import MovieLens
from model.bookcrossing import BookCrossing
from model.randomwalk_sampler import CooccurrenceDataset, CooccurrenceNodeFlowGenerator
from model.randomwalk_sampler import NodeDataset, NodeFlowGenerator, to_device

if torch.cuda.is_available():
    device = torch.device('cuda:0')
else:
    device = torch.device('cpu')

# Argument parsing
parser = argparse.ArgumentParser()
parser.add_argument('--n-epoch', type=int, default=200)
parser.add_argument('--iters-per-epoch', type=int, default=20000)
parser.add_argument('--batch-size', type=int, default=32)
parser.add_argument('--feature-size', type=int, default=16)
parser.add_argument('--n-layers', type=int, default=2)
parser.add_argument('--n-traces', type=int, default=10)
parser.add_argument('--trace-len', type=int, default=3)
parser.add_argument('--n-neighbors', type=int, default=3)
parser.add_argument('--n-negs', type=int, default=4)
parser.add_argument('--weight-decay', type=float, default=1e-5)
parser.add_argument('--margin', type=float, default=1.)
parser.add_argument('--max-c', type=float, default=np.inf)
parser.add_argument('--dataset', type=str, default='movielens')
parser.add_argument('--data-pickle', type=str, default='ml-1m.pkl')
parser.add_argument('--data-path', type=str, default='ml-1m.dataset')
parser.add_argument('--model-path', type=str, default='model.pt')
parser.add_argument('--id-as-feature', action='store_true')
parser.add_argument('--lr', type=float, default=3e-4)
parser.add_argument('--num-workers', type=int, default=0)
parser.add_argument('--pretrain', action='store_true')
parser.add_argument('--neg-by-freq', action='store_true')
parser.add_argument('--neg-freq-min', type=float, default=1)
parser.add_argument('--neg-freq-max', type=float, default=np.inf)
args = parser.parse_args()
n_epoch = args.n_epoch
iters_per_epoch = args.iters_per_epoch
batch_size = args.batch_size
feature_size = args.feature_size
n_layers = args.n_layers
n_traces = args.n_traces
trace_len = args.trace_len
n_neighbors = args.n_neighbors
n_negs = args.n_negs
weight_decay = args.weight_decay
margin = args.margin
max_c = args.max_c
dataset = args.dataset
data_pickle = args.data_pickle
data_path = args.data_path
model_path = args.model_path
id_as_feature = args.id_as_feature
lr = args.lr
num_workers = args.num_workers
pretrain = args.pretrain
neg_by_freq = args.neg_by_freq
neg_freq_max = args.neg_freq_max
neg_freq_min = args.neg_freq_min

# Load the cached dataset object, or parse the raw MovieLens data
if os.path.exists(data_pickle):
    with open(data_pickle, 'rb') as f:
        data = pickle.load(f)
else:
    if dataset == 'movielens':
        data = MovieLens(data_path)
    elif dataset == 'bx':
        data = BookCrossing(data_path)
    with open(data_pickle, 'wb') as f:
        pickle.dump(data, f)

# Fetch the interaction and movie data as numpy arrays
user_latest_item = data.user_latest_item
users_train = data.users_train
movies_train = data.movies_train
users_valid = data.users_valid
movies_valid = data.movies_valid
users_test = data.users_test
movies_test = data.movies_test
train_size = len(users_train)
valid_size = len(users_valid)
test_size = len(users_test)

# Build the bidirectional bipartite graph and put the movie features
HG = dgl.heterograph({
    ('user', 'um', 'movie'): (users_train, movies_train),
    ('movie', 'mu', 'user'): (movies_train, users_train)})
HG.nodes['movie'].data.update(data.movie_data)
HG.to(device)

# Model and optimizer
model_p = PinSage(
        HG, 'movie', 'mu', 'um', feature_size, n_layers, n_neighbors, n_traces,
        trace_len, True, id_as_feature)
model_q = PinSage(
        HG, 'movie', 'mu', 'um', feature_size, n_layers, n_neighbors, n_traces,
        trace_len, True, id_as_feature)
model = nn.ModuleDict({'p': model_p, 'q': model_q})
model = model.to(device)

opt = torch.optim.Adam(model.parameters(), weight_decay=weight_decay, lr=lr)


def cycle_iterator(loader):
    while True:
        it = iter(loader)
        for elem in it:
            yield elem


# pretrain with matrix factorization
if pretrain:
    import tempfile
    tmpfile_train_data = tempfile.NamedTemporaryFile('w+')
    tmpfile_train_model = tmpfile_train_data.name + '.model'

    um = ssp.coo_matrix((np.ones(train_size), (users_train, movies_train)))
    mm = (um.T * um).tocoo()
    for i in tqdm.trange(len(mm.data)):
        print(mm.row[i], mm.col[i], mm.data[i], file=tmpfile_train_data)

    mf_train = sh.Command('libmf/mf-train')
    mf_train('-f', 0, '-k', feature_size, '-t', 500,
             tmpfile_train_data, tmpfile_train_model)

    with open(tmpfile_train_model + '.p', 'w') as f_p, \
         open(tmpfile_train_model + '.q', 'w') as f_q, \
         open(tmpfile_train_model) as f:
        for l in f:
            if l.startswith('p'):
                id_, not_nan, item_data = l[1:].split(' ', 2)
                if not_nan == 'F':
                    print('F in', id_)
                print(item_data, file=f_p)
            elif l.startswith('q'):
                id_, not_nan, item_data = l[1:].split(' ', 2)
                if not_nan == 'F':
                    print('F in', id_)
                print(item_data, file=f_q)

    p = np.loadtxt(tmpfile_train_model + '.p', dtype=np.float32)
    q = np.loadtxt(tmpfile_train_model + '.q', dtype=np.float32)
    model['p'].h.data[:] = torch.FloatTensor(p)
    model['q'].h.data[:] = torch.FloatTensor(q)


def train():
    # count number of occurrences for each movie
    if neg_by_freq:
        um = ssp.coo_matrix((np.ones_like(users_train), (users_train, movies_train)))
        movie_count = torch.FloatTensor(um.sum(0).A.squeeze())
    else:
        movie_count = None

    train_dataset = CooccurrenceDataset(users_train, movies_train)
    valid_dataset = NodeDataset(data.num_movies)
    test_dataset = NodeDataset(data.num_movies)
    train_collator = CooccurrenceNodeFlowGenerator(
            HG, 'um', 'mu', n_neighbors, n_traces, trace_len, model['p'].n_layers, n_negs,
            movie_freq=movie_count, movie_freq_max=neg_freq_max, movie_freq_min=neg_freq_min,
            )
    valid_collator = NodeFlowGenerator(
            HG, 'um', 'mu', n_neighbors, n_traces, trace_len, model['p'].n_layers, n_negs)
    test_collator = NodeFlowGenerator(
            HG, 'um', 'mu', n_neighbors, n_traces, trace_len, model['p'].n_layers, n_negs)
    train_loader = DataLoader(
            train_dataset,
            batch_size=batch_size,
            drop_last=False,
            shuffle=True,
            num_workers=num_workers,
            collate_fn=train_collator)
    valid_loader = DataLoader(
            valid_dataset,
            batch_size=batch_size,
            drop_last=False,
            shuffle=False,
            num_workers=num_workers,
            collate_fn=valid_collator)
    test_loader = DataLoader(
            test_dataset,
            batch_size=batch_size,
            drop_last=False,
            shuffle=False,
            num_workers=num_workers,
            collate_fn=test_collator)
    train_iter = cycle_iterator(train_loader)
    best_metric = None

    baseline_hits_10s = []
    baseline_ndcg_10s = []
    baseline_score_all = data.movie_count

    for u, i in zip(users_test, movies_test):
        I_q = user_latest_item[u]
        I_pos = np.array([i])
        I_neg = data.neg_test[u]
        relevance = np.array([1])

        I = torch.cat([torch.LongTensor(I_pos), torch.LongTensor(I_neg)])
        baseline_score = baseline_score_all[I.numpy()]
        hits_10, ndcg_10 = evaluate(baseline_score, 1, relevance)
        baseline_hits_10s.append(hits_10)
        baseline_ndcg_10s.append(ndcg_10)

    print('HITS@10 (Most popular):', np.mean(baseline_hits_10s),
          'NDCG@10 (Most popular):', np.mean(baseline_ndcg_10s))

    um = np.zeros((data.num_users, data.num_movies))
    um[users_train, movies_train] = 1
    um[users_valid, movies_valid] = 1
    mu = um.T
    m_dist = cosine_similarity(mu)
    baseline_hits_10s = []
    baseline_ndcg_10s = []
    for u, i in zip(users_test, movies_test):
        I_q = user_latest_item[u]
        I_pos = np.array([i])
        I_neg = data.neg_test[u]
        relevance = np.array([1])

        I = torch.cat([torch.LongTensor(I_pos), torch.LongTensor(I_neg)])
        baseline_score = m_dist[I_q][I]
        hits_10, ndcg_10 = evaluate(baseline_score, 1, relevance)
        baseline_hits_10s.append(hits_10)
        baseline_ndcg_10s.append(ndcg_10)

    print('HITS@10 (Item-KNN):', np.mean(baseline_hits_10s),
          'NDCG@10 (Item-KNN):', np.mean(baseline_ndcg_10s))

    for _ in range(n_epoch):
        # train
        sum_loss = 0
        with tqdm.trange(iters_per_epoch) as t:
            for it in t:
                item = next(train_iter)
                I_q, I_i, I_neg, nf_q, nf_i, nf_neg, c = to_device(item, device)

                z_q = model['q'](I_q, nf_q)
                z_i = model['p'](I_i, nf_i)
                z_neg = model['p'](I_neg.view(-1), nf_neg).view(I_neg.shape[0], n_negs, -1)

                score_pos = (z_q * z_i).sum(1)
                score_neg = (z_q.unsqueeze(1) * z_neg).sum(2)
                c = c.clamp(max=max_c)
                loss = (score_neg - score_pos.unsqueeze(1) + margin).clamp(min=0)
                loss = (loss.mean(1) * c).sum() / c.sum()

                opt.zero_grad()
                loss.backward()
                grad_norm = 0
                for name, param in model.named_parameters():
                    d_param = param.grad
                    assert not torch.isnan(d_param).any().item()
                    grad_norm += d_param.norm().item()
                opt.step()

                sum_loss += loss.item()
                t.set_postfix({
                    'loss': '%.06f' % loss.item(),
                    'avg': '%.06f' % (sum_loss / (it + 1)),
                    'gradnorm': '%.06f' % grad_norm})

        with torch.no_grad():
            # evaluate - precompute item embeddings
            z_p = []
            z_q = []
            for item in valid_loader:
                I, nf_i = to_device(item, device)
                z_p.append(model['p'](I, nf_i))
                z_q.append(model['q'](I, nf_i))
            z_p = torch.cat(z_p)
            z_q = torch.cat(z_q)

        hits_10s = []
        ndcg_10s = []

        # evaluate one user-item interaction at a time
        for u, i in zip(users_valid, movies_valid):
            I_q = user_latest_item[u]
            I_pos = np.array([i])
            I_neg = data.neg_valid[u]
            relevance = np.array([1])

            I = torch.cat([torch.LongTensor(I_pos), torch.LongTensor(I_neg)])
            Z_q = z_q[I_q]
            Z = z_p[I]
            score = (Z_q[None, :] * Z).sum(1).cpu().numpy()

            hits_10, ndcg_10 = evaluate(score, 1, relevance)
            hits_10s.append(hits_10)
            ndcg_10s.append(ndcg_10)

        hits_10_valid = np.mean(hits_10s)
        ndcg_10_valid = np.mean(ndcg_10s)

        hits_10s = []
        ndcg_10s = []
        hits_10s_test_all = []
        ndcg_10s_test_all = []
        # evaluate one user-item interaction at a time
        for u, i in zip(users_test, movies_test):
            I_q = user_latest_item[u]
            I_pos = np.array([i])
            I_neg = data.neg_test[u]
            I_neg_all = data.neg_test_complete[u]
            relevance = np.array([1])

            I = torch.cat([torch.LongTensor(I_pos), torch.LongTensor(I_neg)])
            I_all = torch.cat([torch.LongTensor(I_pos), torch.LongTensor(I_neg_all)])
            Z_q = z_q[I_q]
            Z = z_p[I]
            Z_all = z_p[I_all]
            score = (Z_q[None, :] * Z).sum(1).cpu().numpy()
            score_all = (Z_q[None, :] * Z_all).sum(1).cpu().numpy()

            hits_10, ndcg_10 = evaluate(score, 1, relevance)
            hits_10_all, ndcg_10_all = evaluate(score_all, 1, relevance)
            hits_10s.append(hits_10)
            ndcg_10s.append(ndcg_10)
            hits_10s_test_all.append(hits_10_all)
            ndcg_10s_test_all.append(ndcg_10_all)

        hits_10_test = np.mean(hits_10s)
        ndcg_10_test = np.mean(ndcg_10s)
        hits_10_test_all = np.mean(hits_10s_test_all)
        ndcg_10_test_all = np.mean(ndcg_10s_test_all)

        torch.save(model.state_dict(), model_path)

        print('HITS@10:', hits_10_valid, 'NDCG@10:', ndcg_10_valid,
              'HITS@10 (Test):', hits_10_test, 'NDCG@10 (Test):', ndcg_10_test,
              'HITS@10 (Test All):', hits_10_test_all, 'NDCG@10 (Test All):', ndcg_10_test_all,
              )

train()
