import logging
from functools import partial
from typing import Iterable, Optional, Sequence, Union

import numpy as np
import pandas as pd
import torch
from anndata import AnnData
from sklearn.covariance import EllipticEnvelope
from torch.distributions import Categorical, Normal

from scvi._compat import Literal
from scvi._docs import doc_differential_expression
from scvi._utils import _doc_params
from scvi.model._utils import (
    _get_batch_code_from_category,
    _get_var_names_from_setup_anndata,
    scrna_raw_counts_properties,
)
from scvi.model.base._utils import _de_core

logger = logging.getLogger(__name__)
Number = Union[int, float]


class DEMixin:
    """
    DE module relying on Importance-sampling.

    Mixin for using
    importance-weighted DE content.
    This however requires some additional structure on the
    module's (e.g., VAE) methods and associate signatures
    """

    @_doc_params(
        doc_differential_expression=doc_differential_expression,
    )
    def differential_expression(
        self,
        adata: Optional[AnnData] = None,
        groupby: Optional[str] = None,
        group1: Optional[Iterable[str]] = None,
        group2: Optional[str] = None,
        idx1: Optional[Union[Sequence[int], Sequence[bool], str]] = None,
        idx2: Optional[Union[Sequence[int], Sequence[bool], str]] = None,
        mode: Literal["vanilla", "change"] = "change",
        delta: float = 0.25,
        batch_size: Optional[int] = None,
        all_stats: bool = True,
        batch_correction: bool = False,
        batchid1: Optional[Iterable[str]] = None,
        batchid2: Optional[Iterable[str]] = None,
        fdr_target: float = 0.05,
        silent: bool = False,
        pseudocounts: float = 0.0,
        fn_kwargs: Optional[dict] = None,
        importance_sampling: Optional[bool] = False,
        **kwargs,
    ) -> pd.DataFrame:
        r"""
        A unified method for differential expression analysis.

        Implements `"vanilla"` DE [Lopez18]_ and `"change"` mode DE [Boyeau19]_.
        When using the change method, uses either the plugin estimator
        or importance sampling for improved FDR control.

        Parameters
        ----------
        {doc_differential_expression}
        **kwargs
            Keyword args for :func:`scvi.utils.DifferentialComputation.get_bayes_factors`

        Returns
        -------
        Differential expression DataFrame.
        """
        adata = self._validate_anndata(adata)
        adata.uns["_scvi"]["_requires_validation"] = False
        fn_kwargs = dict() if fn_kwargs is None else fn_kwargs
        col_names = _get_var_names_from_setup_anndata(adata)
        if importance_sampling:
            model_fn = partial(
                self.get_population_expression,
                return_numpy=True,
                batch_size=batch_size,
                **fn_kwargs,
            )
        else:
            model_fn = partial(
                self.get_normalized_expression,
                return_numpy=True,
                n_samples=1,
                batch_size=batch_size,
            )

        result = _de_core(
            adata,
            model_fn,
            groupby,
            group1,
            group2,
            idx1,
            idx2,
            all_stats,
            scrna_raw_counts_properties,
            col_names,
            mode,
            batchid1,
            batchid2,
            delta,
            batch_correction,
            fdr_target,
            silent,
            pseudocounts=pseudocounts,
            use_permutation=True,
            **kwargs,
        )

        return result

    @torch.no_grad()
    def get_population_expression(
        self,
        adata: Optional[AnnData] = None,
        indices: Optional[Sequence[int]] = None,
        n_samples: int = 25,
        n_samples_overall: int = None,
        batch_size: Optional[int] = 64,
        filter_cells: bool = True,
        transform_batch: Optional[Sequence[Union[Number, str]]] = None,
        return_numpy: Optional[bool] = False,
        marginal_n_samples_per_pass: int = 500,
        n_mc_samples_px: int = 5000,
        n_cells_per_chunk: Optional[int] = 500,
        max_chunks: Optional[int] = None,
        normalized_expression_key: str = "px_scale",
    ) -> np.ndarray:
        """
        Computes importance-weighted expression levels within a subpopulation.

        There are three majors steps to obtain the expression levels.
        A first optional step consists in filtering out outlier cells, using the cells' latent representation.
        Next, we infer how the data should be split to compute expression levels.
        In particular, if the considered subpopulation is big, we divide the indices
        into K chunks

        Finally, we compute importance-weighted expression levels within each chunk, in `_inference_loop`,
        which we then concatenate and return.

        Parameters
        ----------
        adata
            Anndata to use, defaults to None, by default None
        indices
            Indices of the subpopulation, by default None
        n_samples
            Indices of the subpopulation, by default 25
        n_samples_overall
            Indices of the subpopulation, by default None
        batch_size
            Batch size of the data loader, by default 64
        filter_cells
            Whether cells should be filtered using outlier detection, by default True
        transform_batch
            Batch to use, by default None
        return_numpy
             Whether numpy should be returned, by default False
        marginal_n_samples_per_pass
            Number of samples per pass to compute the marginal likelihood, by default 500
        n_mc_samples_px
            Number of overall samples per cell used to compute the marginal likelihood, by default 5000
        n_cells_per_chunk
            Number of cells to use in each minibatch, by default 500
        max_chunks
            Maximum number of chunks to use, by default None
        normalized_expression_key
            Key associated to the normalized expression level in the model

        Returns
        -------
        res
            Importance weighted expression levels of shaoe (Number of samples, number of genes)
        """
        # Step 1: Determine effective indices to use
        # adata = self._validate_anndata(adata)
        if transform_batch is not None:
            adata_key = self.scvi_setup_dict_["data_registry"]["batch_indices"][
                "attr_key"
            ]
            observed_batches = adata[indices].obs[adata_key].values
            transform_batch_val = _get_batch_code_from_category(adata, transform_batch)[
                0
            ]
            if indices.shape != observed_batches.shape:
                raise ValueError("Discrepancy between # of indices and # of batches")
            indices_ = indices[observed_batches == transform_batch_val]
        else:
            indices_ = indices
        if len(indices_) == 0:
            n_genes = adata.n_vars
            return np.array([]).reshape((0, n_genes))
        if filter_cells:
            indices_ = self.filter_cells(
                adata=adata,
                indices=indices_,
                batch_size=batch_size,
            )

        # Step 2.
        # Determine number of cell chunks
        # Because of the quadratic complexity we split cells in smaller chunks when
        # the cell population is too big
        # This ensures that the method remains scalable when looking at very large cell populations
        n_cells = indices_.shape[0]
        logger.debug("n cells {}".format(n_cells))
        logger.debug(indices_)
        n_cell_chunks = int(np.ceil(n_cells / n_cells_per_chunk))
        np.random.seed(0)
        np.random.shuffle(indices_)
        cell_chunks = np.array_split(indices_, n_cell_chunks)[:max_chunks]
        n_cells_used = np.concatenate(cell_chunks).shape[0]
        # Determine number of samples to generate per cell
        if n_samples_overall is not None:
            n_samples_per_cell = int(1 + np.ceil(n_samples_overall / n_cells_used))
        else:
            n_samples_per_cell = n_samples
            n_samples_overall = n_samples_per_cell * n_cells_used
        n_samples_per_cell = np.minimum(n_samples_per_cell, 100)

        # Step 3
        res = []
        for chunk in cell_chunks:
            logger.debug("n cells chunk {}".format(chunk.shape[0]))
            res.append(
                self._inference_loop(
                    adata=adata,
                    indices=chunk,
                    n_samples=n_samples_per_cell,
                    batch_size=batch_size,
                    marginal_n_samples_per_pass=marginal_n_samples_per_pass,
                    n_mc_samples_px=n_mc_samples_px,
                    normalized_expression_key=normalized_expression_key,
                )["hs_weighted"].numpy()
            )
            logger.debug(res[-1].shape)
        res = np.concatenate(res, 0)
        idx = np.arange(len(res))
        idx = np.random.choice(idx, size=n_samples_overall, replace=True)
        res = res[idx]
        return res

    @torch.no_grad()
    def _inference_loop(
        self,
        adata: AnnData,
        indices: Sequence,
        n_samples: int,
        marginal_n_samples_per_pass: int = 500,
        n_mc_samples_px: int = 5000,
        batch_size: int = 64,
        normalized_expression_key: str = "px_scale",
    ) -> dict:
        """
        Obtain gene expression and densities.

        Computes gene normalized expression samples, as well as
        variational posterior densities and likelihoods for each samples and each
        cell.

        The functioning of the method is a follows.
        For each cell of the dataset, we sample `n_samples` posterior samples.
        We then compute the likelihood of each sample and FOR EACH CELL, using
        `_evaluate_likelihood`.
        After concatenating likelihoods and samples, we derive overall
        importance weights and importance weighted expression levels.

        Parameters
        ----------
        adata
            Considered anndataset
        indices
            Indices of the subpopulation
        n_samples
            Number of posterior samples per cell
        marginal_n_samples_per_pass
            Number of samples per pass to compute the marginal likelihood, by default 500
        n_mc_samples_px
            Number of overall samples per cell used to compute the marginal likelihood, by default 5000
        batch_size
            Number of cells per minibatch, by default 64
        normalized_expression_key
            Key associated to the normalized expression level in the model

        Returns
        -------
        dict
            Containing expression levels, z samples as well as associated densities
        """
        scdl = self._make_data_loader(
            adata=adata, indices=indices, batch_size=batch_size, shuffle=False
        )

        zs = []
        qzs_m = []
        qzs_v = []
        hs = []
        log_px_zs = []
        for tensors in scdl:
            inference_outputs, generative_outputs, = self.module.forward(
                tensors,
                inference_kwargs=dict(n_samples=n_samples, return_densities=True),
                compute_loss=False,
            )
            z = inference_outputs["z"].reshape(-1, self.module.n_latent).cpu()
            h = generative_outputs[normalized_expression_key]
            n_genes = h.shape[-1]
            h = h.reshape(-1, n_genes).cpu()

            zs.append(z)
            qzs_m.append(inference_outputs["qz_m"].cpu())
            qzs_v.append(inference_outputs["qz_v"].cpu())
            hs.append(h)

            _log_px_zs = self._evaluate_likelihood(scdl, inference_outputs)
            log_px_zs.append(_log_px_zs)
        log_px_zs = torch.cat(log_px_zs, 0)
        zs = torch.cat(zs, dim=0)  # shape n_samples, n_cells, n_latent
        hs = torch.cat(hs, dim=0)
        qzs_m = torch.cat(qzs_m, dim=0)
        qzs_v = torch.cat(qzs_v, dim=0)

        _zs = zs.unsqueeze(1)  # shape (overall samples, 1, n_latent)
        log_qz = Normal(qzs_m, qzs_v.sqrt()).log_prob(_zs).sum(-1)
        log_pz = (
            Normal(torch.zeros_like(zs), torch.ones_like(zs))
            .log_prob(zs)
            .sum(-1)
            .squeeze()
        )

        log_px = self.get_marginal_ll(
            adata=adata,
            indices=indices,
            n_mc_samples=n_mc_samples_px,
            n_samples_per_pass=marginal_n_samples_per_pass,
            batch_size=batch_size,
            observation_specific=True,
        )

        importance_weight = torch.logsumexp(
            log_pz.unsqueeze(1)
            + log_px_zs
            - log_px
            - torch.logsumexp(log_qz, 1, keepdims=True),
            dim=1,
        )

        log_probs = importance_weight - torch.logsumexp(importance_weight, 0)
        ws = log_probs.exp()
        n_samples_overall = ws.shape[0]
        windices = (
            Categorical(logits=log_probs.unsqueeze(0))
            .sample((n_samples_overall,))
            .squeeze(-1)
        )
        return dict(
            log_px_zs=log_px_zs,
            log_qz=log_qz,
            log_pz=log_pz,
            log_probs=log_probs,
            probs=ws,
            zs=zs,
            hs=hs,
            hs_weighted=hs[windices],
            qzs_m=qzs_m,
            qzs_v=qzs_v,
        )

    @torch.no_grad()
    def _evaluate_likelihood(self, scdl, inference_outputs) -> torch.Tensor:
        r"""
        Derive required likelihoods.

        Computes :math:`p(x \mid z)`, :math:`q(z \mid x)` as well as :math:`p(z)` for
        each cell :math:`x` contained in `scdl` and predetermined
        posterior samples :math:`z` in `inference_outputs`.
        These quantities are necessary to evalute subpopulation-wide importance weights.

        Parameters
        ----------
        scdl
             Dataset containing cells of interest
        inference_outputs
            Output of module containing the latent variables z of interest

        Returns
        -------
        indices
            Likelihoods of shape (number of cells, number of posterior samples)
        """
        z_samples = inference_outputs["z"]
        if self.module.use_observed_lib_size:
            lib_key = "library"
        else:
            lib_key = "ql_m"
        _z = z_samples.unsqueeze(1).reshape(-1, 1, self.module.n_latent)
        _n_samples_loop = _z.shape[0]
        _log_px_zs = []
        for _tensors in scdl:
            # This is simply used to get a good library value for the cells we are looking at
            _inf_inputs = self.module._get_inference_input(_tensors)
            _n_cells = _inf_inputs["batch_index"].shape[0]

            _z_reshaped = _z.expand(_n_samples_loop, _n_cells, self.module.n_latent)

            point_library = (
                self.module.inference(**_inf_inputs, return_densities=True)[lib_key]
                .squeeze(0)
                .expand(_n_samples_loop, _n_cells, 1)
            )

            inference_outputs["z"] = _z_reshaped
            inference_outputs["library"] = point_library
            _log_px_zs.append(
                self.module.generative_evaluate(
                    tensors=_tensors, inference_outputs=inference_outputs
                )["log_px_latents"].cpu()
            )
        _log_px_zs = torch.cat(_log_px_zs, 1)
        return _log_px_zs

    def filter_cells(
        self, adata: AnnData, indices: Sequence, batch_size: int
    ) -> Sequence:
        """
        Filter outlier cells indexed by indices.

        Parameters
        ----------
        adata
            Anndata containing the observations.
        indices
            Indices characterizing the considered subpopulation.
        batch_size
            Batch-size to use to compute the latent representation.

        Returns
        -------
        indices
            Indices to keep for differential expression
        """
        qz_m = self.get_latent_representation(
            adata=adata,
            indices=indices,
            give_mean=True,
            batch_size=batch_size,
        )
        if (qz_m.ndim != 2) or (qz_m.shape[0] != len(indices)):
            raise ValueError("Dimension mismatch of variational density means")
        try:
            idx_filt = EllipticEnvelope().fit_predict(qz_m)
            idx_filt = idx_filt == 1
        except ValueError:
            logger.warning("Could not properly estimate Cov!, using all samples")
            idx_filt = np.ones(qz_m.shape[0], dtype=bool)
        if (idx_filt == 1).sum() <= 1:
            idx_filt = np.ones(qz_m.shape[0], dtype=bool)
        try:
            indices = indices[idx_filt]
        except IndexError:
            raise IndexError((idx_filt))
        return indices
