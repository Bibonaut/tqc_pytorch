import numpy as np
import torch
from torch.nn import Module, Linear
from torch.distributions import Distribution, Normal
from torch.nn.functional import relu, logsigmoid


from multiq import DEVICE


LOG_STD_MIN_MAX = (-20, 2)


class Mlp(Module):
    def __init__(
            self,
            input_size,
            hidden_sizes,
            output_size
    ):
        super().__init__()
        # TODO: initialization
        self.fcs = []
        in_size = input_size
        for i, next_size in enumerate(hidden_sizes):
            fc = Linear(in_size, next_size)
            self.add_module(f'fc{i}', fc)
            self.fcs.append(fc)
            in_size = next_size
        self.last_fc = Linear(in_size, output_size)

    def forward(self, input):
        h = input
        for fc in self.fcs:
            h = relu(fc(h))
        output = self.last_fc(h)
        return output


class ReplayBuffer(object):
    def __init__(self, state_dim, action_dim, max_size=int(1e6)):
        self.max_size = max_size
        self.ptr = 0
        self.size = 0

        self.transition_names = ('state', 'action', 'next_state', 'reward', 'not_done')
        sizes = (state_dim, action_dim, state_dim, 1, 1)
        for name, size in zip(self.transition_names, sizes):
            setattr(self, name, np.empty((max_size, size)))

    def add(self, state, action, next_state, reward, done):
        values = (state, action, next_state, reward, 1. - done)
        for name, value in zip(self.transition_names, values):
            getattr(self, name)[self.ptr] = value

        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

    def sample(self, batch_size):
        ind = np.random.randint(0, self.size, size=batch_size)
        names = self.transition_names
        return (torch.FloatTensor(getattr(self, name)[ind]).to(DEVICE) for name in names)


class Critic(Module):
    def __init__(self, state_dim, action_dim, n_quantiles, n_nets, depth, width):
        super().__init__()
        self.nets = []
        self.small_nets = []
        self.n_quantiles = n_quantiles
        self.n_nets = n_nets
        for i in range(n_nets):
            self.small_nets.append([])
            net = Mlp(state_dim + action_dim, [512] * (3 - depth), 512)
            self.add_module(f'qf{i}', net)
            self.nets.append(net)
            for j in range(n_quantiles):
                small_net = Mlp(512, [width] * depth, 1)
                self.small_nets[-1].append(small_net)
                self.add_module(f'qf_small_{i}_{j}', small_net)

    def forward(self, state, action):
        sa = torch.cat((state, action), dim=1)
        quantiles_list = []
        for i in range(self.n_nets):
            general_part = self.nets[i](sa)
            output = torch.cat(tuple(small_net(general_part) for small_net in self.small_nets[i]), dim=1)
            quantiles_list.append(output)
        quantiles = torch.stack(quantiles_list, dim=1)
        return quantiles


class Actor(Module):
    def __init__(self, state_dim, action_dim, max_action):
        super().__init__()
        self.action_dim = action_dim
        self.max_action = max_action
        self.net = Mlp(state_dim, [256, 256], 2 * action_dim)

    def forward(self, obs):
        mean, log_std = self.net(obs).split([self.action_dim, self.action_dim], dim=1)
        log_std = log_std.clamp(*LOG_STD_MIN_MAX)

        if self.training:
            std = torch.exp(log_std)
            tanh_normal = TanhNormal(mean, std)
            action, pre_tanh = tanh_normal.rsample()
            action = action * self.max_action
            log_prob = tanh_normal.log_prob(pre_tanh)
            log_prob = log_prob.sum(dim=1, keepdim=True)
        else:  # deterministic eval without log_prob computation
            action = torch.tanh(mean) * self.max_action
            log_prob = None
        return action, log_prob

    def select_action(self, obs):
        obs = torch.FloatTensor(obs).to(DEVICE)[None, :]
        action, _ = self.forward(obs)
        action = action[0].cpu().detach().numpy()
        return action


class TanhNormal(Distribution):
    def __init__(self, normal_mean, normal_std):
        super().__init__()
        self.normal_mean = normal_mean
        self.normal_std = normal_std
        self.standard_normal = Normal(torch.zeros_like(self.normal_mean, device=DEVICE),
                                      torch.ones_like(self.normal_std, device=DEVICE))
        self.normal = Normal(normal_mean, normal_std)

    def log_prob(self, pre_tanh):
        log_det = 2 * np.log(2) + logsigmoid(2 * pre_tanh) + logsigmoid(-2 * pre_tanh)
        result = self.normal.log_prob(pre_tanh) - log_det
        return result

    def rsample(self):
        pretanh = self.normal_mean + self.normal_std * self.standard_normal.sample()
        return torch.tanh(pretanh), pretanh
