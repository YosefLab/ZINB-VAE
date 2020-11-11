# -*- coding: utf-8 -*-
"""Main module."""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal, LogNormal, kl_divergence as kl
from scvi.models.log_likelihood import log_zinb_positive, log_nb_positive
from scvi.models.modules import Encoder, DecoderSCVI, LinearDecoderSCVI
from scvi.models.utils import one_hot
from scvi.models.vae import VAE
import pdb
import time
import numpy as np
from numba import jit

torch.backends.cudnn.benchmark = True

from ete3 import Tree

# TreeVAE Model
class TreeVAE(VAE):
    r"""Model class for fitting a VAE to scRNA-seq data with a tree prior.

    This is corresponding VAE class for our TreeTrainer & implements the TreeVAE model. This model
    performs training in a very specific way, in an effort to respect the tree structure. Specifically,
    we'll perform training of this model by identifying 'clades' (or groups of leaves underneath a given
    internal node) from which the cell's RNA-seq data is assumed to be iid. This is currently done crudely
    by treating every internal node at depth 3 from the root as an appropriate location to create a clade,
    though this should be improved (see TODOs).

    After creating a clustered subtree (where now the leaves correspond to the nodes where clades were induced),
    our training procedure is relativley simple. For every one of these new leaves, split the cells in this clade
    into train/test/validation and in each iteration sample a single cell from the appropriate list and assign its
    RNAseq profile to the clade's root (i.e., the leaf in the clusterd subtree).

    There are a couple of items to clean up here:

    TODO:
        - Find a more ideal way to cluster cells together into clades (currently, we're just
         using depth = 3 as the clustering rule)
        - Ensure that all math here is correct.
        - Implement the ability to sample from the posterior distribution (this is necessary for
        inferring ancestral, or unobserved, transcriptomic profiles)

	"""

    def __init__(
        self,
        n_input: int,
        n_batch: int = 0,
        n_labels: int = 0,
        n_hidden: int = 128,
        n_latent: int = 10,
        n_layers: int = 1,
        dropout_rate: float = 0.1,
        dispersion: str = "gene",
        log_variational: bool = True,
        reconstruction_loss: str = "zinb",
        tree: Tree = None,
        use_clades: bool = None
    ):

        super().__init__(
            n_input,
            n_batch,
            n_labels,
            n_hidden,
            n_latent,
            n_layers,
            dropout_rate,
            dispersion,
            log_variational,
            reconstruction_loss
        )

        def cut_tree(node, distance):

            return node.distance == distance

        self.use_clades = use_clades
        if self.use_clades:
            # Cluster tree into clades: After a certain depth (here = 3), all children nodes are assumed iid and grouped into
            # "clades", for the training we sample one instance of each clade.
            collapsed_tree = Tree(tree.write(is_leaf_fn=lambda x: cut_tree(x, 3)))
            for l in collapsed_tree.get_leaves():
                l.cells = tree.search_nodes(name=l.name)[0].get_leaf_names()
            self.root = collapsed_tree.name
            inf_tree = Tree("prior_root;")
            inf_tree.add_child(collapsed_tree)


        else:
            # No collapsing for simulations (and small trees)
            for l in tree.get_leaves():
                l.cells = tree.search_nodes(name=l.name)[0].get_leaf_names()
            self.root = tree.name
            # add prior node
            inf_tree = Tree("prior_root;")
            inf_tree.add_child(tree)

        self.prior_root = inf_tree.name
        self.tree = inf_tree

        # leaves barcodes
        self.barcodes = [l.name for l in self.tree.get_leaves()]

        #loss function
        self.loss = {}
        self.loss['Reconstruction'], self.loss['MP_lik'], self.loss['Gaussian pdf'] = [], [], []

    def initialize_messages(self, evidence, barcodes, d):

        # at inference, torch tensors are detached
        if type(evidence) == np.ndarray:
            evidence = torch.from_numpy(evidence)

        dic_nu = {}
        dic_mu = {}
        dic_log_z = {}

        for i, j in enumerate(evidence):
            dic_nu[barcodes[i]] = 0
            dic_log_z[barcodes[i]] = 0
            dic_mu[barcodes[i]] = j

        dic_nu[self.prior_root] = 0
        dic_mu[self.prior_root] = torch.from_numpy(np.zeros(d)).type(torch.DoubleTensor)
        dic_log_z[self.prior_root] = 0

        ### ????????????? level-order
        for n in self.tree.traverse():
            if n.name in dic_nu:
                n.add_features(
                    nu=dic_nu[n.name],
                    mu=dic_mu[n.name].type(torch.DoubleTensor),
                    log_z=dic_log_z[n.name],
                )
            else:
                n.add_features(
                    nu=0,
                    mu=torch.from_numpy(np.zeros(d)).type(torch.DoubleTensor),
                    log_z=0,
                )

    def initialize_visit(self):

        for node in self.tree.traverse():
            node.add_features(visited=False)

    def perform_message_passing(self, root_node, d, include_prior):
        # flag the node as visited

        prior_node = self.tree & self.prior_root
        root_node.visited = True

        incoming_messages = []
        incident_nodes = [c for c in root_node.children]
        if not root_node.is_root():
            incident_nodes += [root_node.up]

        # get list of neighbors that are not visited yet
        for node in incident_nodes:
            if not node.visited and (
                node != prior_node or (node == prior_node and include_prior)
            ):
                self.perform_message_passing(node, d, include_prior)
                incoming_messages.append(node)

        n = len(incoming_messages)
        # collect and return
        if n == 0:
            # nothing to do. This happens on the leaves
            return None

        elif n == 1:
            # this happens when passing through the root
            k = incoming_messages[0]
            root_node.nu = k.nu + 1.0  #root_node.get_distance(k)
            root_node.mu = k.mu
            root_node.log_z = 0

        elif n >= 2:
            # we will keep track of mean and variances of the children nodes in 2 lists
            children_nu = [0] * n
            children_mu = [0] * n

            # code profiling
            t0 = time.time()
            for i in range(n):
                k = incoming_messages[i]
                # nu
                children_nu[i] = k.nu + 1.0  #root_node.get_distance(k)
                root_node.nu += 1. / children_nu[i]
                # mu
                children_mu[i] = k.mu / children_nu[i]
                root_node.mu += children_mu[i]

            root_node.nu = 1. / root_node.nu
            root_node.mu *= root_node.nu
            #print("mu & nu took {} seconds".format(time.time() - t0))

            def product_without(L, exclude):
                """
                L: list of elements
                exclude: list of the elements indices to exlucde

                returns: product of all desired array elements
                """
                prod = 1
                for idx, x in enumerate(L):
                    if idx in exclude:
                        continue
                    else:
                        prod *= x
                return prod


            # find t
            t = 0
            for excluded_idx in range(n):
                prod = product_without(children_nu, [excluded_idx])
                t += prod

            # normalizing constants
            Z_1 = -0.5 * (n - 1) * d * np.log(2 * np.pi)
            Z_2 = -0.5 * d * np.log(t)
            Z_3 = 0

            # nested for loop --> need to optimize with numba jit
            t0 = time.time()
            for j in range(n):
                for h in range(n):
                    if h == j:
                        continue
                    else:
                        prod_2 = product_without(children_nu, [j, h])
                k = incoming_messages[h]
                l = incoming_messages[j]
                Z_3 += prod_2 * torch.sum((k.mu - l.mu) ** 2).item()
            Z_3 *= -0.5 / t
            #print("Computing Normalizing constants took {}".format(time.time() - t0))

            root_node.log_z = Z_1 + Z_2 + Z_3

        else:
            # Here there is a problem, we might have tried to compute something weird
            raise NotImplementedError("This should not happen (more than 3). Node" + str(root_node))

    def aggregate_messages_into_leaves_likelihood(self, d, add_prior):
        res = 0

        root_node = self.tree & self.root

        # agg Z messages
        for node in self.tree.traverse():
            res += node.log_z

        if add_prior:
            # add prior
            nu_inc = 1 + root_node.nu
            res += -0.5 * torch.sum(
                root_node.mu ** 2
            ).item() / nu_inc - d * 0.5 * np.log(2 * np.pi * nu_inc)
        return res


    def posterior_predictive_density(self, query_node, evidence=None):
        """
        :param query_node: (string) barcode of a query node
               evidence: (ndarray) observation values at the leaves (used as an initialization)
        :return: the expectation and the variance for the posterior (distribution query_node | observations)
        """

        root_node = self.tree & self.root

        self.initialize_visit()

        if evidence is not None:
            self.initialize_messages(evidence,
                                     self.barcodes,
                                     self.n_latent)

        self.perform_message_passing((self.tree & query_node), len(root_node.mu), True)
        return (self.tree & query_node).mu, (self.tree & query_node).nu


    def inference(
        self, x, batch_index=None, y=None, n_samples=1
    ):
        """Helper function used in forward pass
                """

        # Sampling
        qz_m, qz_v, z = self.z_encoder(x, y)

        # we consider the library size fixed
        ql_m, ql_v, library = self.l_encoder(x)

        if n_samples > 1:
            qz_m = qz_m.unsqueeze(0).expand((n_samples, qz_m.size(0), qz_m.size(1)))
            qz_v = qz_v.unsqueeze(0).expand((n_samples, qz_v.size(0), qz_v.size(1)))
            # when z is normal, untran_z == z
            untran_z = Normal(qz_m, qz_v.sqrt()).sample()
            z = self.z_encoder.z_transformation(untran_z)


            #ql_m = ql_m.unsqueeze(0).expand((n_samples, ql_m.size(0), ql_m.size(1)))
            #ql_v = ql_v.unsqueeze(0).expand((n_samples, ql_v.size(0), ql_v.size(1)))
            #library = Normal(ql_m, ql_v.sqrt()).sample()

        dec_batch_index = batch_index

        # Library size fixed
        library = torch.log(x.sum(dim=1,
                        dtype=torch.float64
                        )).view(-1, 1)

        px_scale, px_r, px_rate, px_dropout = self.decoder(
            self.dispersion, z, library, dec_batch_index, y
        )
        if self.dispersion == "gene-label":
            px_r = F.linear(
                one_hot(y, self.n_labels), self.px_r
            )  # px_r gets transposed - last dimension is nb genes
        elif self.dispersion == "gene-batch":
            px_r = F.linear(one_hot(dec_batch_index, self.n_batch), self.px_r)
        elif self.dispersion == "gene":
            px_r = self.px_r
        px_r = torch.exp(px_r)

        return dict(
            px_scale=px_scale,
            px_r=px_r,
            px_rate=px_rate,
            px_dropout=px_dropout,
            qz_m=qz_m,
            qz_v=qz_v,
            z=z,
            ql_m=None,
            ql_v=None,
            library=library,
        )


    def forward(
        self, x, local_l_mean, local_l_var, batch_index=None, y=None, barcodes=None
    ):
        r""" Returns the reconstruction loss

		:param x: tensor of values with shape (batch_size, n_input)
		:param local_l_mean: tensor of means of the prior distribution of latent variable l
		 with shape (batch_size, 1)
		:param local_l_var: tensor of variancess of the prior distribution of latent variable l
		 with shape (batch_size, 1)
		:param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
		:param y: tensor of cell-types labels with shape (batch_size, n_labels)
		:return: the reconstruction loss and the Kullback divergences
		:rtype: 2-tuple of :py:class:`torch.FloatTensor`
		"""
        # Parameters for z latent distribution
        outputs = self.inference(x, batch_index, y)

        qz_m = outputs["qz_m"]
        qz_v = outputs["qz_v"]
        ql_m = outputs["ql_m"]
        ql_v = outputs["ql_v"]
        px_rate = outputs["px_rate"]
        px_r = outputs["px_r"]
        px_dropout = outputs["px_dropout"]
        z = outputs["z"]
        library = outputs["library"]

        # message passing likelihood
        self.initialize_visit()
        self.initialize_messages(
            z,
            self.barcodes,
            self.n_latent
        )

        self.perform_message_passing((self.tree & self.root), z.shape[1], False)
        mp_lik = self.aggregate_messages_into_leaves_likelihood(
            z.shape[1], add_prior=True
        )

        qz = Normal(qz_m, torch.sqrt(qz_v)).log_prob(z).sum(dim=-1)

        # library size likelihood
        # pl = LogNormal(ql_m, torch.sqrt(ql_v)).log_prob(l).sum(dim=1)

        self.loss['MP_lik'].append(mp_lik / z.shape[0])
        self.loss['Reconstruction'].append(torch.mean(self.get_reconstruction_loss(x, px_rate, px_r, px_dropout)).item())
        self.loss['Gaussian pdf'].append(torch.mean(qz).item())

        reconst_loss = (
            self.get_reconstruction_loss(x, px_rate, px_r, px_dropout) + qz
        )

        return reconst_loss, mp_lik
