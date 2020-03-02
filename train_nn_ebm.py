# coding=utf-8
# Copyright 2019 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import utils
import torch as t, torch.nn as nn, torch.nn.functional as tnnF, torch.distributions as tdist
from torch.utils.data import DataLoader, Dataset
import torchvision as tv, torchvision.transforms as tr
import os
import sys
import argparse
#import ipdb
import numpy as np
import wideresnet
import json
# Sampling
from tqdm import tqdm
t.backends.cudnn.benchmark = True
t.backends.cudnn.enabled = True
seed = 1
# im_sz = 32
# n_ch = 3
from sklearn import datasets
import matplotlib.pyplot as plt



class DataSubset(Dataset):
    def __init__(self, base_dataset, inds=None, size=-1):
        self.base_dataset = base_dataset
        if inds is None:
            inds = np.random.choice(list(range(len(base_dataset))), size, replace=False)
        self.inds = inds

    def __getitem__(self, index):
        base_ind = self.inds[index]
        return self.base_dataset[base_ind]

    def __len__(self):
        return len(self.inds)


class NeuralNet(nn.Module):
    def __init__(self, input_size, hidden_size, extra_layers=2):
        super(NeuralNet, self).__init__()
        self.layers = nn.ModuleList()

        layer_in = nn.Linear(input_size, hidden_size)
        self.layers.append(layer_in)
        for i in range(extra_layers):
            self.layers.append(nn.Linear(hidden_size, hidden_size))
        # self.layer_out = nn.Linear(hidden_size, output_size)
        # self.layers.append(layer_out)

        self.relu = nn.ReLU()

    def forward(self, x, y=None):
        if len(x.shape) > 2:
            x = x.reshape(-1, x.shape[-1]**2)
        for layer in self.layers:
            x = layer(x)
            x = self.relu(x)
        # logits = self.layer_out(x)s
        output = x
        return output


class F(nn.Module):
    def __init__(self, depth=28, width=2, norm=None, dropout_rate=0.0, im_sz=32, use_nn=False, input_size=None, n_classes=10):
        if input_size is not None:
            assert use_nn == True #input size is for non-images, ie non-conv.
        super(F, self).__init__()

        if use_nn:
            hidden_units = 500
            if input_size is None:
                self.f = NeuralNet(im_sz**2, hidden_units, extra_layers=2)
            else:
                self.f = NeuralNet(input_size, hidden_units, extra_layers=2)
            self.f.last_dim = hidden_units
        else:
            self.f = wideresnet.Wide_ResNet(depth, width, norm=norm, dropout_rate=dropout_rate)

        self.energy_output = nn.Linear(self.f.last_dim, 1)
        self.class_output = nn.Linear(self.f.last_dim, n_classes)

    def forward(self, x, y=None):
        penult_z = self.f(x)
        return self.energy_output(penult_z).squeeze()

    def classify(self, x):
        penult_z = self.f(x)
        return self.class_output(penult_z).squeeze()


class CCF(F):
    def __init__(self, depth=28, width=2, norm=None, dropout_rate=0.0, im_sz=32, use_nn=False, input_size=None, n_classes=10):
        super(CCF, self).__init__(depth, width, norm=norm, dropout_rate=dropout_rate, n_classes=n_classes, im_sz=im_sz, input_size=input_size, use_nn=use_nn)

    def forward(self, x, y=None):
        logits = self.classify(x)
        if y is None:
            return logits.logsumexp(1)
        else:
            return t.gather(logits, 1, y[:, None])


def _l2_normalize(d):
    d_reshaped = d.view(d.shape[0], -1, *(1 for _ in range(d.dim() - 2)))
    d /= t.norm(d_reshaped, dim=1, keepdim=True) + 1e-8
    return d

class VATLoss(nn.Module):
    # Source https://github.com/lyakaap/VAT-pytorch/blob/master/vat.py

    def __init__(self, xi=10.0, eps=1.0, ip=1):
        """VAT loss
        :param xi: hyperparameter of VAT (default: 10.0)
        :param eps: hyperparameter of VAT (default: 1.0)
        :param ip: iteration times of computing adv noise (default: 1)
        """
        super(VATLoss, self).__init__()
        self.xi = xi
        self.eps = eps
        self.ip = ip

    def forward(self, model, x):
        with t.no_grad():
            pred = t.nn.functional.softmax(model.classify(x), dim=1)

        # prepare random unit tensor
        d = t.rand(x.shape).sub(0.5).to(x.device)
        d = _l2_normalize(d)

        # calc adversarial direction
        for _ in range(self.ip):
            d.requires_grad_()
            pred_hat = model.classify(x + self.xi * d)
            logp_hat = t.nn.functional.log_softmax(pred_hat, dim=1)
            adv_distance = t.nn.functional.kl_div(logp_hat, pred, reduction='batchmean')
            adv_distance.backward()
            d = _l2_normalize(d.grad)
            model.zero_grad()

        # calc LDS
        r_adv = d * self.eps
        pred_hat = model.classify(x + r_adv)
        logp_hat = t.nn.functional.log_softmax(pred_hat, dim=1)
        lds = t.nn.functional.kl_div(logp_hat, pred, reduction='batchmean')

        return lds



def cycle(loader):
    while True:
        for data in loader:
            yield data


def grad_norm(m):
    total_norm = 0
    for p in m.parameters():
        param_grad = p.grad
        if param_grad is not None:
            param_norm = param_grad.data.norm(2) ** 2
            total_norm += param_norm
    total_norm = total_norm ** (1. / 2)
    return total_norm.item()


def grad_vals(m):
    ps = []
    for p in m.parameters():
        if p.grad is not None:
            ps.append(p.grad.data.view(-1))
    ps = t.cat(ps)
    return ps.mean().item(), ps.std(), ps.abs().mean(), ps.abs().std(), ps.abs().min(), ps.abs().max()


def init_random(args, bs):
    if args.dataset == "moons":
        out = t.FloatTensor(bs, args.input_size).uniform_(-1,1)
    else:
        out = t.FloatTensor(bs, args.n_ch, args.im_sz, args.im_sz).uniform_(-1, 1)
    return out

def get_model_and_buffer(args, device, sample_q):
    model_cls = F if args.uncond else CCF
    args.input_size = None
    if args.dataset == "mnist" or args.dataset == "moons":
        use_nn=True
        # use_nn=False # testing only
        if args.dataset == "moons":
            args.input_size = 2
    else:
        use_nn=False
    f = model_cls(args.depth, args.width, args.norm, dropout_rate=args.dropout_rate, n_classes=args.n_classes, im_sz=args.im_sz, input_size=args.input_size, use_nn=use_nn)
    if not args.uncond:
        assert args.buffer_size % args.n_classes == 0, "Buffer size must be divisible by args.n_classes"
    if args.load_path is None:
        # make replay buffer
        replay_buffer = init_random(args, args.buffer_size)
    else:
        print(f"loading model from {args.load_path}")
        ckpt_dict = t.load(args.load_path)
        f.load_state_dict(ckpt_dict["model_state_dict"])
        replay_buffer = ckpt_dict["replay_buffer"]

    f = f.to(device)
    return f, replay_buffer


def logit_transform(x, lamb = 0.05):
    # Adapted from https://github.com/yookoon/VLAE
    x = (x * 255.0 + t.rand_like(x)) / 256.0 # noise
    x = lamb + (1 - 2.0 * lamb) * x # clipping to avoid explosion at ends
    x = t.log(x) - t.log(1.0 - x)
    return x


def get_data(args):
    if args.dataset == "svhn":
        transform_train = tr.Compose(
            [tr.Pad(4, padding_mode="reflect"),
             tr.RandomCrop(args.im_sz),
             tr.ToTensor(),
             tr.Normalize((.5, .5, .5), (.5, .5, .5)),
             lambda x: x + args.sigma * t.randn_like(x)]
        )
    elif args.dataset == "mnist":
        transform_train = tr.Compose(
            [tr.Pad(4, padding_mode="reflect"),
             tr.RandomCrop(args.im_sz),
             tr.ToTensor(),
             # tr.Normalize((0.5,), (0.5,)),
             # lambda x: x + args.sigma * t.randn_like(x)
             logit_transform
             ]
        )
    elif args.dataset == "moons":
        transform_train = None
    else:
        transform_train = tr.Compose(
            [tr.Pad(4, padding_mode="reflect"),
             tr.RandomCrop(args.im_sz),
             tr.RandomHorizontalFlip(),
             tr.ToTensor(),
             tr.Normalize((.5, .5, .5), (.5, .5, .5)),
             lambda x: x + args.sigma * t.randn_like(x)]
        )
    if args.dataset == "mnist":
        transform_test = tr.Compose(
            [tr.ToTensor(),
             # tr.Normalize((.5,), (.5,)),
             # lambda x: x + args.sigma * t.randn_like(x)
             logit_transform
            ]
        )
    elif args.dataset == "moons":
        transform_test = None
    else:
        transform_test = tr.Compose(
            [tr.ToTensor(),
             tr.Normalize((.5, .5, .5), (.5, .5, .5)),
             lambda x: x + args.sigma * t.randn_like(x)]
        )
    def dataset_fn(train, transform):
        if args.dataset == "cifar10":
            return tv.datasets.CIFAR10(root=args.data_root, transform=transform, download=True, train=train)
        elif args.dataset == "cifar100":
            return tv.datasets.CIFAR100(root=args.data_root, transform=transform, download=True, train=train)
        elif args.dataset == "mnist":
            return tv.datasets.MNIST(root=args.data_root, transform=transform, download=True, train=train)
        elif args.dataset == "moons":
            data,labels = datasets.make_moons(n_samples=args.n_moons_data, noise=.1)

            # plt.scatter(data[:,0],data[:,1])
            # plt.show()
            data = t.Tensor(data)

            labels = t.Tensor(labels)
            labels = labels.long()
            return t.utils.data.TensorDataset(data, labels)
        else:
            return tv.datasets.SVHN(root=args.data_root, transform=transform, download=True,
                                    split="train" if train else "test")



    # get all training inds
    full_train = dataset_fn(True, transform_train)
    all_inds = list(range(len(full_train)))
    # set seed
    np.random.seed(1234)
    # shuffle
    np.random.shuffle(all_inds)
    # seperate out validation set
    if args.n_valid is not None:
        valid_inds, train_inds = all_inds[:args.n_valid], all_inds[args.n_valid:]
    else:
        valid_inds, train_inds = [], all_inds
    train_inds = np.array(train_inds)
    train_labeled_inds = []
    other_inds = []

    train_labels = np.array([full_train[ind][1] for ind in train_inds])
    if args.labels_per_class > 0:
        for i in range(args.n_classes):
            print(i)
            train_labeled_inds.extend(train_inds[train_labels == i][:args.labels_per_class])
            other_inds.extend(train_inds[train_labels == i][args.labels_per_class:])
    else:
        train_labeled_inds = train_inds

    dset_train = DataSubset(
        dataset_fn(True, transform_train),
        inds=train_inds)
    dset_train_labeled = DataSubset(
        dataset_fn(True, transform_train),
        inds=train_labeled_inds)
    dset_valid = DataSubset(
        dataset_fn(True, transform_test),
        inds=valid_inds)
    dload_train = DataLoader(dset_train, batch_size=args.batch_size, shuffle=True, num_workers=4, drop_last=True)
    dload_train_labeled = DataLoader(dset_train_labeled, batch_size=args.batch_size, shuffle=True, num_workers=4, drop_last=True)
    dload_train_labeled = cycle(dload_train_labeled)
    dset_test = dataset_fn(False, transform_test)
    dload_valid = DataLoader(dset_valid, batch_size=100, shuffle=False, num_workers=4, drop_last=False)
    dload_test = DataLoader(dset_test, batch_size=100, shuffle=False, num_workers=4, drop_last=False)
    return dload_train, dload_train_labeled, dload_valid,dload_test, dset_train, dset_train_labeled


def get_sample_q(args, device):
    def sample_p_0(replay_buffer, bs, y=None):
        if len(replay_buffer) == 0:
            return init_random(args, bs), []
        buffer_size = len(replay_buffer) if y is None else len(replay_buffer) // args.n_classes
        inds = t.randint(0, buffer_size, (bs,))
        # if cond, convert inds to class conditional inds
        if y is not None:
            inds = y.cpu() * buffer_size + inds
            assert not args.uncond, "Can't drawn conditional samples without giving me y"
        buffer_samples = replay_buffer[inds]
        random_samples = init_random(args, bs)
        if args.dataset == "moons":
            choose_random = (t.rand(bs) < args.reinit_freq).float()[:, None]
        else:
            choose_random = (t.rand(bs) < args.reinit_freq).float()[:, None, None, None]
        samples = choose_random * random_samples + (1 - choose_random) * buffer_samples
        return samples.to(device), inds

    def sample_q(f, replay_buffer, y=None, n_steps=args.n_steps):
        """this func takes in replay_buffer now so we have the option to sample from
        scratch (i.e. replay_buffer==[]).  See test_wrn_ebm.py for example.
        """
        f.eval()
        # get batch size
        bs = args.batch_size if y is None else y.size(0)
        # generate initial samples and buffer inds of those samples (if buffer is used)
        init_sample, buffer_inds = sample_p_0(replay_buffer, bs=bs, y=y)
        x_k = t.autograd.Variable(init_sample, requires_grad=True)
        # sgld
        for k in range(n_steps):
            f_prime = t.autograd.grad(f(x_k, y=y).sum(), [x_k], retain_graph=True)[0]
            x_k.data += args.sgld_lr * f_prime + args.sgld_std * t.randn_like(x_k)
        f.train()
        final_samples = x_k.detach()
        # update replay buffer
        if len(replay_buffer) > 0:
            replay_buffer[buffer_inds] = final_samples.cpu()
        return final_samples
    return sample_q


def eval_classification(f, dload, device):
    corrects, losses = [], []
    for x_p_d, y_p_d in dload:
        x_p_d, y_p_d = x_p_d.to(device), y_p_d.to(device)
        logits = f.classify(x_p_d)
        loss = nn.CrossEntropyLoss(reduce=False)(logits, y_p_d).cpu().numpy()
        losses.extend(loss)
        correct = (logits.max(1)[1] == y_p_d).float().cpu().numpy()
        corrects.extend(correct)
    loss = np.mean(losses)
    correct = np.mean(corrects)
    return correct, loss


def checkpoint(f, buffer, tag, args, device):
    f.cpu()
    ckpt_dict = {
        "model_state_dict": f.state_dict(),
        "replay_buffer": buffer
    }
    t.save(ckpt_dict, os.path.join(args.save_dir, tag))
    f.to(device)


def main(args):
    utils.makedirs(args.save_dir)
    with open(f'{args.save_dir}/params.txt', 'w') as f:
        json.dump(args.__dict__, f)
    if args.print_to_log:
        sys.stdout = open(f'{args.save_dir}/log.txt', 'w')

    t.manual_seed(seed)
    if t.cuda.is_available():
        t.cuda.manual_seed_all(seed)

    if args.dataset == "mnist":
        args.n_ch = 1
        args.im_sz = 28
    elif args.dataset == "moons":
        args.n_ch = None
        args.im_sz = None
    else:
        args.n_ch = 3
        args.im_sz = 32

    # datasets
    dload_train, dload_train_labeled, dload_valid, dload_test, dset_train, dset_train_labeled = get_data(args)

    device = t.device('cuda' if t.cuda.is_available() else 'cpu')

    sample_q = get_sample_q(args, device)
    f, replay_buffer = get_model_and_buffer(args, device, sample_q)

    sqrt = lambda x: int(t.sqrt(t.Tensor([x])))
    plot = lambda p, x: tv.utils.save_image(t.clamp(x, -1, 1), p, normalize=True, nrow=sqrt(x.size(0)))

    # optimizer
    params = f.class_output.parameters() if args.clf_only else f.parameters()
    if args.optimizer == "adam":
        optim = t.optim.Adam(params, lr=args.lr, betas=[.9, .999], weight_decay=args.weight_decay)
    else:
        optim = t.optim.SGD(params, lr=args.lr, momentum=.9, weight_decay=args.weight_decay)

    best_valid_acc = 0.0
    cur_iter = 0
    for epoch in range(args.n_epochs):
        if epoch in args.decay_epochs:
            for param_group in optim.param_groups:
                new_lr = param_group['lr'] * args.decay_rate
                param_group['lr'] = new_lr
            print("Decaying lr to {}".format(new_lr))
        for i, (x_p_d, _) in tqdm(enumerate(dload_train)):
            if cur_iter <= args.warmup_iters:
                lr = args.lr * cur_iter / float(args.warmup_iters)
                for param_group in optim.param_groups:
                    param_group['lr'] = lr

            x_p_d = x_p_d.to(device)
            x_lab, y_lab = dload_train_labeled.__next__()
            x_lab, y_lab = x_lab.to(device), y_lab.to(device)

            L = 0.

            if args.vat:

                # if args.class_cond_p_x_sample:
                #     assert not args.uncond, "can only draw class-conditional samples if EBM is class-cond"
                #     y_q = t.randint(0, args.n_classes, (args.batch_size,)).to(
                #         device)
                #     x_q = sample_q(f, replay_buffer, y=y_q)
                # else:
                #     x_q = sample_q(f, replay_buffer)

                optim.zero_grad()
                vat_loss = VATLoss(xi=10.0, eps=1.0, ip=1)
                lds = vat_loss(f, x_p_d)
                # lds = vat_loss(f, x_q)

                # lds = vat_loss(f.classify, x_q)

                logits = f.classify(x_lab)

                loss = args.p_y_given_x_weight * nn.CrossEntropyLoss()(logits, y_lab) + args.vat_weight * lds

                loss.backward()
                optim.step()

                cur_iter += 1

                if cur_iter % args.print_every == 0:
                    acc = (logits.max(1)[1] == y_lab).float().mean()
                    print(
                        'P(y|x) {}:{:>d} loss={:>14.9f}, acc={:>14.9f}'.format(
                            epoch,
                            cur_iter,
                            loss.item(),
                            acc.item()))

            else:

                if args.p_x_weight > 0:  # maximize log p(x)
                    if args.class_cond_label_prop:
                        assert args.class_cond_p_x_sample, "need class-conditional samples for psuedo label prop"
                    if args.class_cond_p_x_sample:
                        assert not args.uncond, "can only draw class-conditional samples if EBM is class-cond"
                        y_q = t.randint(0, args.n_classes, (args.batch_size,)).to(device)
                        x_q = sample_q(f, replay_buffer, y=y_q)
                        if args.class_cond_label_prop and cur_iter > args.warmup_iters:
                            logits_pseudo = f.classify(x_q)
                            l_p_y_given_pseudo_x = nn.CrossEntropyLoss()(logits_pseudo, y_q)
                            L += args.label_prop_weight * l_p_y_given_pseudo_x
                            if cur_iter % args.print_every == 0:
                                acc = (logits_pseudo.max(1)[1] == y_q).float().mean()
                                print(
                                    'Pseudo_P(y|x) {}:{:>d} loss={:>14.9f}, acc={:>14.9f}'.format(
                                        epoch,
                                        cur_iter,
                                        l_p_y_given_pseudo_x.item(),
                                        acc.item()))
                    else:
                        x_q = sample_q(f, replay_buffer)  # sample from log-sumexp

                    # TODO the arg is alredy here, and the smapling is already done too. All I need to do is add the update based on cross entropy for new samples too (just extend the classifier batch?)

                    fp_all = f(x_p_d)
                    fq_all = f(x_q)
                    fp = fp_all.mean()
                    fq = fq_all.mean()

                    l_p_x = -(fp - fq)
                    if cur_iter % args.print_every == 0:
                        print('P(x) | {}:{:>d} f(x_p_d)={:>14.9f} f(x_q)={:>14.9f} d={:>14.9f}'.format(epoch, i, fp, fq,
                                                                                                       fp - fq))
                    L += args.p_x_weight * l_p_x

                if args.p_y_given_x_weight > 0:  # maximize log p(y | x)
                    logits = f.classify(x_lab)
                    l_p_y_given_x = nn.CrossEntropyLoss()(logits, y_lab)



                    if cur_iter % args.print_every == 0:
                        acc = (logits.max(1)[1] == y_lab).float().mean()
                        print('P(y|x) {}:{:>d} loss={:>14.9f}, acc={:>14.9f}'.format(epoch,
                                                                                     cur_iter,
                                                                                     l_p_y_given_x.item(),
                                                                                     acc.item()))

                        if args.svd_jacobian:
                            # Let's just do 1 example for now
                            input_ex_ind = 0
                            x_example = x_lab[input_ex_ind]
                            x_example.requires_grad = True
                            j_list = []
                            for i in range(args.n_classes):
                                grad = t.autograd.grad(f.classify(x_example)[i],
                                                       x_example)[0]
                                grad = grad.reshape(-1)
                                j_list.append(grad)
                            jacobian = t.stack(j_list)
                            u, s, v = t.svd(jacobian)
                            # print(jacobian.shape)
                            # print(u)
                            # print(u.shape)
                            print(s)
                            # print(s.shape)
                            # print(v)
                            # print(v.shape)
                            spectrum = s.detach().cpu().numpy()
                            plt.figure()
                            plt.scatter(np.arange(0, args.n_classes), spectrum)
                            plt.savefig("spectrum_digit{}_epoch{}".format(y_lab[input_ex_ind], epoch))
                            plt.clf()

                    L += args.p_y_given_x_weight * l_p_y_given_x

                if args.p_x_y_weight > 0:  # maximize log p(x, y)
                    assert not args.uncond, "this objective can only be trained for class-conditional EBM DUUUUUUUUHHHH!!!"
                    x_q_lab = sample_q(f, replay_buffer, y=y_lab)
                    fp, fq = f(x_lab, y_lab).mean(), f(x_q_lab, y_lab).mean()
                    l_p_x_y = -(fp - fq)
                    if cur_iter % args.print_every == 0:
                        print('P(x, y) | {}:{:>d} f(x_p_d)={:>14.9f} f(x_q)={:>14.9f} d={:>14.9f}'.format(epoch, i, fp, fq,
                                                                                                          fp - fq))

                    L += args.p_x_y_weight * l_p_x_y

                # break if the loss diverged...easier for poppa to run experiments this way
                if L.abs().item() > 1e8:
                    print("BAD BOIIIIIIIIII")
                    1/0

                optim.zero_grad()
                L.backward()
                optim.step()

                cur_iter += 1

                if cur_iter % 100 == 0:
                    if args.plot_uncond:
                        if args.class_cond_p_x_sample:
                            assert not args.uncond, "can only draw class-conditional samples if EBM is class-cond"
                            y_q = t.randint(0, args.n_classes, (args.batch_size,)).to(device)
                            x_q = sample_q(f, replay_buffer, y=y_q)
                        else:
                            x_q = sample_q(f, replay_buffer)
                        plot('{}/x_q_{}_{:>06d}.png'.format(args.save_dir, epoch, i), x_q)
                    if args.plot_cond:  # generate class-conditional samples
                        y = t.arange(0, args.n_classes)[None].repeat(args.n_classes, 1).transpose(1, 0).contiguous().view(-1).to(device)
                        x_q_y = sample_q(f, replay_buffer, y=y)
                        plot('{}/x_q_y{}_{:>06d}.png'.format(args.save_dir, epoch, i), x_q_y)

        if epoch % args.ckpt_every == 0:
            checkpoint(f, replay_buffer, f'ckpt_{epoch}.pt', args, device)

        if epoch % args.eval_every == 0 and (args.p_y_given_x_weight > 0 or args.p_x_y_weight > 0):
            f.eval()
            with t.no_grad():
                # validation set
                correct, loss = eval_classification(f, dload_valid, device)
                print("Epoch {}: Valid Loss {}, Valid Acc {}".format(epoch, loss, correct))
                if correct > best_valid_acc:
                    best_valid_acc = correct
                    print("Best Valid!: {}".format(correct))
                    checkpoint(f, replay_buffer, "best_valid_ckpt.pt", args, device)
                # test set
                correct, loss = eval_classification(f, dload_test, device)
                print("Epoch {}: Test Loss {}, Test Acc {}".format(epoch, loss, correct))
            f.train()

            if args.dataset == "moons" and correct >= best_valid_acc:
                data,labels= datasets.make_moons(args.n_moons_data, noise=0.1)
                data = t.Tensor(data)
                preds = f.classify(data.to(device))
                preds = preds.argmax(dim=1)
                # preds = preds.detach().numpy()
                # print(preds)
                preds = preds.cpu()
                data1 = data[preds == 0]
                # print(data1)
                plt.scatter(data1[:,0], data1[:,1], c="orange")
                data2 = data[preds == 1]
                # print(data2)
                plt.scatter(data2[:,0], data2[:,1], c="blue")

                # labeled_pts = []
                # data, labels = dload_train_labeled.__next__()
                # labeled_pts = data
                # for i in range(2-1):
                #     # labeled_pts.append(data)
                #     data,labels = dload_train_labeled.__next__()
                #     labeled_pts = np.vstack((labeled_pts, data))
                # print(labeled_pts)
                labeled_pts = dset_train_labeled[:][0]
                labeled_pts_labels = dset_train_labeled[:][1]
                labeled0 = labeled_pts[labeled_pts_labels == 0]
                labeled1 = labeled_pts[labeled_pts_labels == 1]
                # Note labels right now not forced to be class balanced
                # print(sum(labeled_pts_labels))
                plt.scatter(labeled0[:,0], labeled0[:,1], c="green")
                plt.scatter(labeled1[:,0], labeled1[:,1], c="red")
                print("Saving figure")
                plt.savefig("moonsvis.png")
                # plt.show()

        checkpoint(f, replay_buffer, "last_ckpt.pt", args, device)



if __name__ == "__main__":
    parser = argparse.ArgumentParser("Energy Based Models and Shit")
    #cifar
    parser.add_argument("--dataset", type=str, default="moons", choices=["cifar10", "svhn", "mnist", "cifar100", "moons"])
    parser.add_argument("--data_root", type=str, default="../data")
    # optimization
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--decay_epochs", nargs="+", type=int, default=[160, 180],
                        help="decay learning rate by decay_rate at these epochs")
    parser.add_argument("--decay_rate", type=float, default=.3,
                        help="learning rate decay multiplier")
    parser.add_argument("--clf_only", action="store_true", help="If set, then only train the classifier")
    #labels was -1?
    # parser.add_argument("--labels_per_class", type=int, default=-1,
    #                     help="number of labeled examples per class, if zero then use all labels")
    parser.add_argument("--labels_per_class", type=int, default=10,
                        help="number of labeled examples per class, if zero then use all labels")
    parser.add_argument("--optimizer", choices=["adam", "sgd"], default="adam")
    # parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--batch_size", type=int, default=10)
    parser.add_argument("--n_epochs", type=int, default=200)
    parser.add_argument("--warmup_iters", type=int, default=-1,
                        help="number of iters to linearly increase learning rate, if -1 then no warmmup")
    # loss weighting
    parser.add_argument("--p_x_weight", type=float, default=1.)
    parser.add_argument("--p_y_given_x_weight", type=float, default=1.)
    parser.add_argument("--label_prop_weight", type=float, default=1.)
    parser.add_argument("--p_x_y_weight", type=float, default=0.)
    # regularization
    parser.add_argument("--dropout_rate", type=float, default=0.0)
    parser.add_argument("--sigma", type=float, default=3e-2,
                        help="stddev of gaussian noise to add to input, .03 works but .1 is more stable")
    parser.add_argument("--weight_decay", type=float, default=0.0)
    # network
    parser.add_argument("--norm", type=str, default=None, choices=[None, "norm", "batch", "instance", "layer", "act"],
                        help="norm to add to weights, none works fine")
    # EBM specific
    parser.add_argument("--n_steps", type=int, default=20,
                        help="number of steps of SGLD per iteration, 100 works for short-run, 20 works for PCD")
    parser.add_argument("--width", type=int, default=10, help="WRN width parameter")
    parser.add_argument("--depth", type=int, default=28, help="WRN depth parameter")
    parser.add_argument("--uncond", action="store_true", help="If set, then the EBM is unconditional")
    parser.add_argument("--class_cond_p_x_sample", action="store_true",
                        help="If set we sample from p(y)p(x|y), othewise sample from p(x),"
                             "Sample quality higher if set, but classification accuracy better if not.")
    parser.add_argument("--buffer_size", type=int, default=10000)
    parser.add_argument("--reinit_freq", type=float, default=.05)
    parser.add_argument("--sgld_lr", type=float, default=1.0)
    parser.add_argument("--sgld_std", type=float, default=1e-2)
    # logging + evaluation
    parser.add_argument("--save_dir", type=str, default='./experiment')
    parser.add_argument("--ckpt_every", type=int, default=10, help="Epochs between checkpoint save")
    parser.add_argument("--eval_every", type=int, default=1, help="Epochs between evaluation")
    parser.add_argument("--print_every", type=int, default=100, help="Iterations between print")
    parser.add_argument("--load_path", type=str, default=None)
    parser.add_argument("--print_to_log", action="store_true", help="If true, directs std-out to log file")
    parser.add_argument("--plot_cond", action="store_true", help="If set, save class-conditional samples")
    parser.add_argument("--plot_uncond", action="store_true", help="If set, save unconditional samples")
    parser.add_argument("--n_valid", type=int, default=5000)
    # parser.add_argument("--n_valid", type=int, default=50)
    parser.add_argument("--semi-supervised", type=bool, default=False)
    # parser.add_argument("--vat", type=bool, default=False)
    parser.add_argument("--vat", action="store_true", help="Run VAT instead of JEM")
    parser.add_argument("--vat_weight", type=float, default=1.0)
    parser.add_argument("--n_moons_data", type=float, default=500)
    parser.add_argument("--class_cond_label_prop", action="store_true", help="Train on generated class cond samples too")
    parser.add_argument("--svd_jacobian", action="store_true", help="Do SVD on Jacobian matrix at data points to help understand model behaviour")



    args = parser.parse_args()
    if args.dataset == "cifar100":
        args.n_classes = 100
    elif args.dataset == "moons":
        args.n_classes = 2
    else:
        args.n_classes = 10
    if args.vat:
        print("Running VAT")

    main(args)
