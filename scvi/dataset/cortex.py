import csv
import logging
import os
from typing import List, Optional

import numpy as np

from scvi.dataset.dataset import DownloadableDataset

logger = logging.getLogger(__name__)


class CortexDataset(DownloadableDataset):
    """Loads cortex dataset.

    The `Mouse Cortex Cells dataset`_ contains 3005 mouse cortex cells and gold-standard labels for
    seven distinct cell types. Each cell type corresponds to a cluster to recover. We retain top 558 genes
    ordered by variance.

    :param save_path: Path indicating where to save/load data.
    :param genes_to_keep: Gene names to keep.
    :param total_genes: Total number of genes to keep. If None, use all genes.
    :param delayed_populating: Boolean switch for delayed population mechanism.

    Examples:
        >>> gene_dataset = CortexDataset()

    .. _Mouse Cortex Cells dataset:
        https://storage.googleapis.com/linnarsson-lab-www-blobs/blobs/cortex/expression_mRNA_17-Aug-2014.txt
    """
    def __init__(
        self,
        save_path: str = "data/",
        genes_to_keep: Optional[List[str]] = None,
        total_genes: Optional[int] = 558,
        delayed_populating: bool = False,
    ):
        self.genes_to_keep = genes_to_keep
        self.total_genes = total_genes

        self.precise_labels = None

        super().__init__(
            urls="https://storage.googleapis.com/linnarsson-lab-www-blobs/blobs"
                 "/cortex/expression_mRNA_17-Aug-2014.txt",
            filenames="expression.bin",
            save_path=save_path,
            delayed_populating=delayed_populating,
        )

    def populate(self):
        logger.info("Loading Cortex data")
        rows = []
        gene_names = []
        with open(os.path.join(self.save_path, self.filenames[0]), "r") as csvfile:
            data_reader = csv.reader(csvfile, delimiter="\t")
            for i, row in enumerate(data_reader):
                if i == 1:
                    precise_clusters = np.array(row, dtype=str)[2:]
                if i == 8:
                    clusters = np.asarray(row, dtype=str)[2:]
                if i >= 11:
                    rows.append(row[1:])
                    gene_names.append(row[0])
        cell_types, labels = np.unique(clusters, return_inverse=True)
        _, self.precise_labels = np.unique(precise_clusters, return_inverse=True)
        X = np.array(rows, dtype=np.int).T[1:]
        gene_names = np.array(gene_names, dtype=np.str)
        gene_indices = []
        if self.genes_to_keep is not None:
            _, gene_indices, _ = np.intersect1d(
                self.genes_to_keep, gene_names, return_indices=True
            )

        nb_gene_indices = len(gene_indices)
        extra_gene_indices = []
        if self.total_genes is not None and nb_gene_indices < self.total_genes:
            not_gene_indices_mask = np.ones(X.shape[1], dtype=np.bool)
            not_gene_indices_mask[gene_indices] = False
            genes_by_variance = np.std(X[:, ~not_gene_indices_mask], axis=0).argsort()[::-1]
            extra_gene_indices = genes_by_variance[: self.total_genes - len(gene_indices)]

        gene_indices = np.concatenate([gene_indices, extra_gene_indices])
        if self.total_genes is None and self.genes_to_keep is None:
            gene_indices = slice(None)

        X = X[:, gene_indices]
        gene_names = gene_names[gene_indices]

        logger.info("Finished preprocessing Cortex data")
        self.populate_from_data(
            X=X,
            labels=labels,
            gene_names=gene_names,
            cell_types=cell_types,
            cell_attributes_dict={"precise_labels": precise_clusters}
        )
