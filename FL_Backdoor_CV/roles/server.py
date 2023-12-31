import copy
import math
import os
import random
import re
from collections import defaultdict
from math import pi

import hdbscan
import numpy as np
import sklearn.metrics.pairwise as smp
import torch
import torch.nn.functional as F
from scipy.linalg import eigh as largest_eigh
from sklearn.cluster import DBSCAN
from torch.nn.utils import parameters_to_vector, vector_to_parameters

from FL_Backdoor_CV.models.create_model import create_model
from FL_Backdoor_CV.roles.evaluation import test_cv, test_poison_cv
from configs import args


class Server:
    def __init__(self, helper, clients, adversary_list):
        # === model ===
        if args.resume:
            self.model = torch.load(os.path.join('../saved_models', args.resumed_name),
                                    map_location=args.device)
        else:
            self.model = create_model()

        # === clients, participants, attackers, and benign clients ===
        self.clients = clients
        self.participants = None
        self.adversary_list = adversary_list
        self.benign_indices = list(set(list(range(args.participant_population))) - set(self.adversary_list))

        # === image helper ===
        self.helper = helper

        # === whether resume ===
        self.current_round = 0
        # === Inherent recognition accuracy on poisoned data sets
        self.inheret_poison_acc = 0

        if args.resume:
            self.current_round = int(re.findall(r'\d+\d*', args.resumed_name.split('/')[1])[0])
            test_l, test_acc = self.validate()
            if args.attack_mode == 'COMBINE':
                test_l_acc = self.validate_poison()
                print(f"\n--------------------- T e s t - L o a d e d - M o d e l ---------------------")
                print(f"Accuracy on testset: {test_acc: .4f}, Loss on testset: {test_l: .4f}.")
                for i in range(args.multi_objective_num):
                    if i == 0:
                        print(f"Poison accuracy (o1): {test_l_acc[0][1]: .4f}.", end='   =========   ')
                    elif i == 1:
                        print(f"Poison accuracy (o2): {test_l_acc[1][1]: .4f}.")
                    elif i == 2:
                        print(f"Poison accuracy (o3): {test_l_acc[2][1]: .4f}.", end='   =========   ')
                    elif i == 3:
                        print(f"Poison accuracy (o4): {test_l_acc[3][1]: .4f}.")
                    elif i == 4:
                        print(f"Poison accuracy (wall ---> bird): {test_l_acc[4][1]: .4f}.")
                    elif i == 5:
                        print(f"Poison accuracy (green car ---> bird): {test_l_acc[5][1]: .4f}.")
                    elif i == 6:
                        print(f"Poison accuracy (strip car ---> bird): {test_l_acc[6][1]: .4f}.")
                print(f"--------------------- C o m p l e t e ! ---------------------\n")
            else:
                test_poison_loss, test_poison_acc = self.validate_poison()
                self.inheret_poison_acc = test_poison_acc
                print(f"\n--------------------- T e s t - L o a d e d - M o d e l ---------------------\n"
                      f"Accuracy on testset: {test_acc: .4f}, "
                      f"Loss on testset: {test_l: .4f}. <---> "
                      f"Poison accuracy: {test_poison_acc: .4f}, "
                      f"Poison loss: {test_poison_loss: .4f}"
                      f"\n--------------------- C o m p l e t e ! ---------------------\n")

            # === total data size ===
        self.total_size = 0

        # === whether poison ===
        self.poison_rounds = list()
        self.is_poison = args.is_poison
        if self.is_poison:
            # === give the poison rounds in the configuration ===
            if args.poison_rounds:
                assert isinstance(args.poison_rounds, str)
                self.poison_rounds = [int(i) for i in args.poison_rounds.split(',')]
            else:
                retrain_rounds = np.arange(self.current_round + 1,
                                           self.current_round + args.retrain_rounds + 1)
                whether_poison = np.random.uniform(0, 1, args.retrain_rounds) >= (1 - args.poison_prob)
                self.poison_rounds = set((retrain_rounds * whether_poison).tolist())
                if 0 in self.poison_rounds:
                    self.poison_rounds.remove(0)
                self.poison_rounds = list(self.poison_rounds)
            args.poison_rounds = self.poison_rounds
            print(f"\n--------------------- P o i s o n - R o u n d s : {self.poison_rounds} ---------------------\n")

        # === root dataset ===
        self.root_dataset = None
        if args.aggregation_rule == 'fltrust':
            being_sampled_indices = list(range(args.participant_sample_size))
            subset_data_chunks = random.sample(being_sampled_indices, 1)[0]
            self.root_dataset = self.helper.benign_train_data[subset_data_chunks]

    def select_participants(self):
        self.current_round += 1
        self.total_size = 0
        if args.random_compromise:
            self.participants = random.sample(range(args.participant_population), args.participant_sample_size)
        else:
            if self.current_round in self.poison_rounds:
                if args.attack_mode == 'DBA':
                    candidates = list()
                    adversarial_index = self.poison_rounds.index(self.current_round)
                    for client_id in self.adversary_list:
                        if self.clients[client_id].adversarial_index == adversarial_index:
                            candidates.append(client_id)
                    self.participants = candidates + random.sample(
                        self.benign_indices, args.participant_sample_size - len(candidates))

                    # === calculate the size of participating examples ===
                    for client_id in self.participants:
                        self.total_size += self.clients[client_id].local_data_size
                    print(
                        f"Participants in round {self.current_round}: {[client_id for client_id in self.participants]}, "
                        f"Benign participants: {args.participant_sample_size - len(candidates)}, "
                        f"Total size: {self.total_size}")
                else:
                    self.participants = self.adversary_list + random.sample(
                        self.benign_indices, args.participant_sample_size - len(self.adversary_list))

                    # === calculate the size of participating examples ===
                    for client_id in self.participants:
                        self.total_size += self.clients[client_id].local_data_size
                    print(
                        f"Participants in round {self.current_round}: {[client_id for client_id in self.participants]}, "
                        f"Benign participants: {args.participant_sample_size - len(self.adversary_list)}, "
                        f"Total size: {self.total_size}")
            else:
                self.participants = random.sample(self.benign_indices, args.participant_sample_size)
                # === calculate the size of participating examples ===
                for client_id in self.participants:
                    self.total_size += self.clients[client_id].local_data_size
                print(f"Participants in round {self.current_round}: {[client_id for client_id in self.participants]}, "
                      f"Benign participants: {args.participant_sample_size}, "
                      f"Total size: {self.total_size}")

    def train_and_aggregate(self, global_lr):
        # === trained local models ===
        trained_models = dict()
        param_updates = list()
        trained_params = list()
        for client_id in self.participants:
            local_model = copy.deepcopy(self.model)
            local_model.load_state_dict(self.model.state_dict())
            trained_local_model = self.clients[client_id].local_train(local_model, self.helper, self.current_round)
            if args.aggregation_rule.lower() == 'fltrust':
                param_updates.append(parameters_to_vector(trained_local_model.parameters()) - parameters_to_vector(
                    self.model.parameters()))
            elif args.aggregation_rule.lower() == 'flame':
                # trained_param = None
                # for name, param in trained_local_model.state_dict().items():
                #     if trained_param is not None:
                #         trained_param = torch.cat((trained_param, param.detach().cpu().view(-1)))
                #     else:
                #         trained_param = param.detach().cpu().view(-1)
                trained_param = parameters_to_vector(trained_local_model.parameters()).detach().cpu()
                trained_params.append(trained_param)

            for name, param in trained_local_model.state_dict().items():
                if name not in trained_models:
                    trained_models[name] = param.data.view(1, -1)
                else:
                    trained_models[name] = torch.cat((trained_models[name], param.data.view(1, -1)),
                                                     dim=0)

        # === model updates ===
        model_updates = dict()
        for (name, param), local_param in zip(self.model.state_dict().items(), trained_models.values()):
            model_updates[name] = local_param.data - param.data.view(1, -1)
            if args.attack_mode in ['MR', 'DBA', 'FLIP', 'EDGE_CASE', 'NEUROTOXIN', 'COMBINE']:
                if 'num_batches_tracked' not in name:
                    for i in range(args.participant_sample_size):
                        if self.clients[self.participants[i]].malicious:
                            mal_boost = 1
                            if args.is_poison:
                                if args.mal_boost:
                                    if args.attack_mode in ['MR', 'FLIP', 'EDGE_CASE', 'NEUROTOXIN', 'COMBINE']:
                                        mal_boost = args.mal_boost / args.number_of_adversaries
                                    elif args.attack_mode == 'DBA':
                                        mal_boost = args.mal_boost / (args.number_of_adversaries / args.dba_trigger_num)
                                else:
                                    if args.attack_mode in ['MR', 'FLIP', 'EDGE_CASE', 'NEUROTOXIN', 'COMBINE']:
                                        mal_boost = args.participant_sample_size / args.number_of_adversaries
                                    elif args.attack_mode == 'DBA':
                                        mal_boost = args.participant_sample_size / \
                                                    (args.number_of_adversaries / args.dba_trigger_num)
                            model_updates[name][i] *= mal_boost

        # === aggregate ===
        global_update = None
        if args.aggregation_rule.lower() == 'avg':
            global_update = Server.avg(model_updates)
        elif args.aggregation_rule.lower() == 'roseagg':
            global_update = Server.roseagg(model_updates, current_round=self.current_round)
        elif args.aggregation_rule.lower() == 'foolsgold':
            global_update = Server.foolsgold(model_updates)
        elif args.aggregation_rule.lower() == 'rlr':
            global_update = Server.robust_lr(model_updates)
        elif args.aggregation_rule.lower() == 'fltrust':
            # lr_init = self.helper.params['lr']
            # traget_lr = self.helper.params['target_lr']
            #
            # if self.helper.params['dataset'] == 'emnist':
            #     lr = 0.0001
            #     if self.helper.params['emnist_style'] == 'byclass':
            #         if self.current_round <= 500:
            #             lr = self.current_round * (traget_lr - lr_init) / 499.0 + lr_init - (
            #                         traget_lr - lr_init) / 499.0
            #         else:
            #             lr = self.current_round * (-traget_lr) / 1500 + traget_lr * 4.0 / 3.0
            #
            #             if lr <= 0.0001:
            #                 lr = 0.0001
            # else:
            #     if self.current_round <= 500:
            #         lr = self.current_round * (traget_lr - lr_init) / 499.0 + lr_init - (traget_lr - lr_init) / 499.0
            #     else:
            #         lr = self.current_round * (-traget_lr) / 1500 + traget_lr * 4.0 / 3.0
            #         if lr <= args.local_lr_min:
            #             lr = args.local_lr_min
            lr = args.local_lr
            if self.current_round > 2000:
                lr = lr * args.local_lr_decay ** (self.current_round - 2000)
            if args.is_poison:
                lr = args.local_lr_min
            model = copy.deepcopy(self.model)
            model.load_state_dict(self.model.state_dict())
            optimizer = torch.optim.SGD(model.parameters(), lr=lr,
                                        momentum=self.helper.params['momentum'],
                                        weight_decay=self.helper.params['decay'])
            epochs = self.helper.params['retrain_no_times']
            criterion = torch.nn.CrossEntropyLoss()
            for _ in range(epochs):
                for inputs, labels in self.root_dataset:
                    inputs, labels = inputs.to(args.device), labels.to(args.device)
                    optimizer.zero_grad()
                    loss = criterion(model(inputs), labels)
                    loss.backward()
                    optimizer.step()

            # clean_model_update = dict()
            # for (name, param), root_param in zip(self.model.state_dict().items(), model.state_dict().values()):
            #     clean_model_update[name] = root_param.data.view(1, -1) - param.data.view(1, -1)

            clean_param_update = parameters_to_vector(model.parameters()) - parameters_to_vector(
                self.model.parameters())

            global_update = Server.fltrust(model_updates, param_updates, clean_param_update)
            global_lr = 0.2
        elif args.aggregation_rule.lower() == 'flame':
            # current_model_param = None
            # for name, param in self.model.state_dict().items():
            #     if current_model_param is not None:
            #         current_model_param = torch.cat((current_model_param, param.detach().cpu().view(-1)))
            #     else:
            #         current_model_param = param.detach().cpu().view(-1)
            current_model_param = parameters_to_vector(self.model.parameters()).detach().cpu()
            global_param, global_update = Server.flame(trained_params, current_model_param, model_updates)
            vector_to_parameters(global_param, self.model.parameters())
            model_param = self.model.state_dict()
            for name, param in model_param.items():
                if 'num_batches_tracked' in name or 'running_mean' in name or 'running_var' in name:
                    model_param[name] = param.data + global_update[name].view(param.size())
            self.model.load_state_dict(model_param)
            return

        # === update the global model ===
        model_param = self.model.state_dict()
        for name, param in model_param.items():
            model_param[name] = param.data + global_lr * global_update[name].view(param.size())
        self.model.load_state_dict(model_param)

    def validate(self):
        with torch.no_grad():
            test_l, test_acc = test_cv(self.helper.benign_test_data, self.model)
        return test_l, test_acc

    def validate_poison(self):
        with torch.no_grad():
            if args.attack_mode == 'COMBINE':
                test_l_acc = []
                for i in range(args.multi_objective_num):
                    test_l, test_acc = test_poison_cv(self.helper, self.helper.poisoned_test_data,
                                                      self.model, adversarial_index=i)
                    test_l_acc.append((test_l, test_acc))
                return test_l_acc
            else:
                test_l, test_acc = test_poison_cv(self.helper, self.helper.poisoned_test_data, self.model)
                return test_l, test_acc

    @staticmethod
    def avg(model_updates):
        global_update = dict()
        for name, data in model_updates.items():
            global_update[name] = 1 / args.participant_sample_size * model_updates[name].sum(dim=0, keepdim=True)
        return global_update

    @staticmethod
    def roseagg(model_updates, important_feature_indices=None, current_round=0):

        def sign_distance(sign_1, sign_2):
            sim_signs = len(sign_1) - torch.count_nonzero(torch.tensor(sign_1) - torch.tensor(sign_2))
            sim_ratio = sim_signs / len(sign_1)
            return sim_ratio

        # detect malicious
        mal_idxs = list()

        """
        construct a distance matrix
        """
        keys = list(model_updates.keys())
        last_layer_updates = model_updates[keys[-2]]
        K = len(last_layer_updates)
        distance_matrix = smp.cosine_similarity(last_layer_updates.cpu().numpy()) - np.eye(K)
        # np.savetxt('distance_matrix.txt', distance_matrix)

        # distance_matrix = pairwise_distances(torch.sign(last_layer_updates).cpu().numpy(),
        #                                      metric=sign_distance) - np.eye(K)

        """
        clustering distance matrix using dbscan
        """

        cluster = DBSCAN(eps=args.threshold, min_samples=1, metric='cosine').fit(last_layer_updates.cpu().numpy())
        clusters = dict()
        for i, clu in enumerate(cluster.labels_):
            if clu in clusters:
                clusters[clu] += [i]
            else:
                clusters[clu] = [i]
        for clu in clusters.values():
            if len(clu) > 1:
                mal_idxs.append(clu)
        possible_malicious_idxs = set([idx for idxs in mal_idxs for idx in idxs])
        possible_benign_idxs = list(set(list(range(K))) - possible_malicious_idxs)

        """
        cluster model updates according to the distance matrix
        idxs = np.argwhere(distance_matrix > args.threshold)
        for abnormal_idx in idxs:
            if mal_idxs:
                flags = [0] * len(mal_idxs)
                for i, mal_idx in enumerate(mal_idxs):
                    if mal_idx & set(abnormal_idx):
                        flags[i] = 1
                merge_idxs = np.argwhere(np.array(flags) == 1).reshape(1, -1)[0]
                if len(merge_idxs) > 0:
                    mal_idxs[merge_idxs[0]] = mal_idxs[merge_idxs[0]] | set(abnormal_idx)
                    for merge_idx in merge_idxs[1:]:
                        mal_idxs[merge_idxs[0]] = mal_idxs[merge_idxs[0]] | mal_idxs[merge_idx]
                        mal_idxs.remove(mal_idxs[merge_idx])
                else:
                    mal_idxs.append(set(abnormal_idx))
            else:
                mal_idxs.append(set(abnormal_idx))
        possible_malicious_idxs = set([idx for idxs in mal_idxs for idx in idxs])
        possible_benign_idxs = list(set(list(range(K))) - possible_malicious_idxs)
        """

        """
        indices that need to be aggregated within each cluster
        """
        remove_idxs = []
        for idxs in mal_idxs:
            idxs = list(idxs)
            idxs.sort()
            if remove_idxs:
                remove_idxs.extend(idxs[1:])
            else:
                remove_idxs = idxs[1:]
        reserve_idxs = list(set(list(range(K))) - set(remove_idxs))
        print(f'Possible malicious indices: {mal_idxs}, '
              f'Clusters: {reserve_idxs}, {cluster.labels_}')

        """
        compute a weight vector according to the cluster result
        """
        weights = torch.ones(K)
        sims = dict()
        if mal_idxs:
            for mal_idx in mal_idxs:
                mal_idx = list(mal_idx)
                mal_idx.sort()
                sub_distance_matrix = distance_matrix[mal_idx, :][:, mal_idx]
                sub_weights = sub_distance_matrix.sum(axis=0)
                sub_weights = sub_weights / sub_weights.sum()
                for i, idx in enumerate(mal_idx):
                    sims[idx] = sub_weights[i]
            for i, s in sims.items():
                weights[i] = s

        # calculate global model update
        global_update = defaultdict()
        for name, layer_updates in model_updates.items():
            if 'num_batches_tracked' in name:
                global_update[name] = torch.sum(layer_updates[possible_benign_idxs]) / len(
                    layer_updates[possible_benign_idxs])
            else:
                local_norms = [torch.norm(layer_updates[i]) for i in range(K)]
                local_norms.sort()

                origin_directions = F.normalize(layer_updates)
                origin_directions = weights.view(-1, 1).to(args.device) * origin_directions
                if mal_idxs:
                    # targeted aggregation within each cluster
                    first_idxs = []
                    for idxs in mal_idxs:
                        idxs = list(idxs)
                        idxs.sort()
                        first_idx = idxs[0]
                        first_idxs.append(first_idx)
                        for idx in idxs[1:]:
                            origin_directions[first_idx] = origin_directions[first_idx] + origin_directions[idx]
                        """
                        theta = torch.tensor(np.random.uniform(pi / 6, pi / 3)).to(args.device)
                        direction = torch.normal(0, 1, origin_directions[first_idx].size()).to(args.device)
                        direction /= torch.norm(direction)
                        origin_directions[first_idx] += torch.norm(origin_directions[first_idx]) * torch.tan(
                            theta) * direction
                        """
                        origin_directions[first_idx] /= torch.norm(origin_directions[first_idx])
                    origin_directions = origin_directions[reserve_idxs]
                # calculate the direction of the global update
                N = origin_directions.size(0)
                if args.cia_evaluation:
                    principal_direction = origin_directions.sum(dim=0) / N
                    n = K - args.number_of_adversaries
                    scale = torch.median(torch.tensor(local_norms[:n]))
                    principal_direction *= scale
                else:
                    X = torch.matmul(origin_directions, origin_directions.T)
                    evals_large, evecs_large = largest_eigh(X.detach().cpu().numpy(), eigvals=(N - N, N - 1))
                    evals_large = torch.tensor(evals_large)[-1].to(args.device)
                    evecs_large = torch.tensor(evecs_large)[:, -1].to(args.device)
                    principal_direction = torch.matmul(evecs_large.view(1, -1), origin_directions).T / torch.sqrt(
                        evals_large)
                    new_weights = torch.pow(torch.matmul(principal_direction.view(1, -1), origin_directions.T), 2)
                    new_weights = new_weights / new_weights.sum()
                    n = K - args.number_of_adversaries
                    if args.dataset.lower() == 'cifar10':
                        scale = torch.median(torch.tensor(local_norms[:n])) / N
                    elif args.dataset.lower() == 'fmnist':
                        scale = torch.median(torch.tensor(local_norms[:n]))
                        if current_round > 200:
                            scale = torch.median(torch.tensor(local_norms[:n])) / N
                    else:
                        scale = torch.mean(torch.tensor(local_norms[:n]))
                    origin_directions += torch.normal(0, 0.003, origin_directions.size()).to(args.device)
                    origin_directions = F.normalize(origin_directions)
                    principal_direction = torch.matmul(new_weights, origin_directions)
                    principal_direction /= torch.norm(principal_direction)
                    principal_direction *= scale

                # cos = torch.nn.CosineSimilarity(dim=0)
                # variance = 0
                # if name == 'linear.weight':
                #     for i in range(N):
                #         cos_sim = cos(principal_direction.view(-1, 1), origin_directions[i].view(-1, 1)).item()
                #         print(cos_sim)
                #     variance += cos_sim
                #     # if name == 'linear.weight':
                #     #     print(f'\033[0;35m------------------ {i} >>> {cos_sim} ------------------\033[0m')
                # variance /= N
                # # if name == 'linear.weight':
                # #     print(f'\033[0;35m------------------ variance >>> {variance} ------------------\033[0m')
                #
                # principal_direction *= variance
                # principal_direction *= min(local_norms)

                global_update[name] = principal_direction
        return global_update

    @staticmethod
    def foolsgold(model_updates):
        keys = list(model_updates.keys())
        last_layer_updates = model_updates[keys[-2]]
        K = len(last_layer_updates)
        cs = smp.cosine_similarity(last_layer_updates.cpu().numpy()) - np.eye(K)
        maxcs = np.max(cs, axis=1)
        # pardoning
        for i in range(K):
            for j in range(K):
                if i == j:
                    continue
                if maxcs[i] < maxcs[j]:
                    cs[i][j] = cs[i][j] * maxcs[i] / maxcs[j]

        alpha = np.max(cs, axis=1)
        wv = 1 - alpha
        wv[wv > 1] = 1
        wv[wv < 0] = 0

        # Rescale so that max value is wv
        wv = wv / np.max(wv)
        wv[(wv == 1)] = .99

        # Logit function
        wv = (np.log(wv / (1 - wv)) + 0.5)
        wv[(np.isinf(wv) + wv > 1)] = 1
        wv[(wv < 0)] = 0
        print(f'the weight of each model update: {wv}')
        # calculate global update
        global_update = dict()
        for name in keys:
            tmp = None
            for i, j in enumerate(range(len(wv))):
                if i == 0:
                    tmp = model_updates[name][j] * wv[j]
                else:
                    tmp += model_updates[name][j] * wv[j]
            global_update[name] = 1 / len(wv) * tmp

        return global_update

    @staticmethod
    def robust_lr(model_updates):
        global_update = dict()
        for name, param in model_updates.items():
            if 'num_batches_tracked' in name or 'running_mean' in name or 'running_var' in name:
                global_update[name] = 1 / args.participant_sample_size * \
                                      model_updates[name].sum(dim=0, keepdim=True)
            else:
                signs = torch.sign(model_updates[name])
                sm_of_signs = torch.abs(torch.sum(signs, dim=0, keepdim=True))
                sm_of_signs[sm_of_signs < args.robustLR_threshold] = -1
                sm_of_signs[sm_of_signs >= args.robustLR_threshold] = 1
                global_update[name] = 1 / args.participant_sample_size * \
                                      (sm_of_signs * model_updates[name].sum(dim=0, keepdim=True))
        return global_update

    @staticmethod
    def fltrust(model_updates, param_updates, clean_param_update):
        cos = torch.nn.CosineSimilarity(dim=0)
        g0_norm = torch.norm(clean_param_update)
        weights = []
        for param_update in param_updates:
            weights.append(F.relu(cos(param_update.view(-1, 1), clean_param_update.view(-1, 1))))
        weights = torch.tensor(weights).to(args.device).view(1, -1)
        weights = weights / weights.sum()
        weights = torch.where(weights[0].isnan(), torch.zeros_like(weights), weights)
        print(weights.cpu().data)

        normalize_weights = []
        for param_update in param_updates:
            normalize_weights.append(g0_norm / torch.norm(param_update))

        global_update = dict()
        for name, params in model_updates.items():
            if 'num_batches_tracked' in name or 'running_mean' in name or 'running_var' in name:
                global_update[name] = 1 / args.participant_sample_size * params.sum(dim=0, keepdim=True)
            else:
                global_update[name] = torch.matmul(weights,
                                                   params * torch.tensor(normalize_weights).to(args.device).view(-1, 1))
        return global_update

    @staticmethod
    def flame(trained_params, current_model_param, param_updates):
        # === clustering ===
        trained_params = torch.stack(trained_params).double()
        cluster = hdbscan.HDBSCAN(metric="cosine", algorithm="generic",
                                  min_cluster_size=args.participant_sample_size // 2 + 1,
                                  min_samples=1, allow_single_cluster=True)
        cluster.fit(trained_params)
        predict_good = []
        for i, j in enumerate(cluster.labels_):
            if j == 0:
                predict_good.append(i)

        # === median clipping ===
        model_updates = trained_params[predict_good] - current_model_param
        local_norms = torch.norm(model_updates, dim=1)
        S_t = torch.median(local_norms)
        scale = S_t / local_norms
        scale = torch.where(scale > 1, torch.ones_like(scale), scale)
        model_updates = model_updates * scale.view(-1, 1)

        # === aggregating ===
        trained_params = current_model_param + model_updates
        trained_params = trained_params.sum(dim=0) / len(trained_params)

        # === noising ===
        delta = 1 / (args.participant_sample_size ** 2)
        epsilon = 300
        lambda_ = 1 / epsilon * (math.sqrt(2 * math.log((1.25 / delta))))
        sigma = lambda_ * S_t.numpy()
        print(f"sigma: {sigma}; clean models: {predict_good}, median norm: {S_t}")
        trained_params.add_(torch.normal(0, sigma, size=trained_params.size()))

        # === bn ===
        global_update = dict()
        for name, param in param_updates.items():
            if 'num_batches_tracked' in name:
                global_update[name] = 1 / args.participant_sample_size * \
                                      param_updates[name][predict_good].sum(dim=0, keepdim=True)
            elif 'running_mean' in name or 'running_var' in name:
                local_norms = torch.norm(param_updates[name][predict_good], dim=1)
                S_t = torch.median(local_norms)
                scale = S_t / local_norms
                scale = torch.where(scale > 1, torch.ones_like(scale), scale)
                global_update[name] = param_updates[name][predict_good] * scale.view(-1, 1)
                global_update[name] = 1 / args.participant_sample_size * global_update[name].sum(dim=0, keepdim=True)

        return trained_params.float().to(args.device), global_update
