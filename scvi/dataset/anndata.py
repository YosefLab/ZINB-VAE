from .dataset import GeneExpressionDataset
import anndata
import numpy as np


class AnnDataset(GeneExpressionDataset):
    r""" Loads a `.h5ad` file.

    `AnnDataset` class supports loading `Anndata`_ object.

    Args:
        filename (str): Name of the `.h5ad` file
        save_path (str, optional): Save path of the dataset
        url (str, optional): Url of the remote dataset
        new_n_genes (int, optional): Number of subsampled genes
        subset_genes (list, optional): List of genes for subsampling

    Examples:
        >>> # Loading a local dataset
        >>> local_ann_dataset = AnnDataset("TM_droplet_mat.h5ad", save_path = 'data/')

    .. _Anndata:
        http://anndata.readthedocs.io/en/latest/

    """

    def __init__(self, download_name, save_path='data/', url=None, new_n_genes=False, subset_genes=None):
        self.download_name = download_name
        self.save_path = save_path
        self.url = url

        data, gene_names = self.download_and_preprocess()

        super(AnnDataset, self).__init__(*GeneExpressionDataset.get_attributes_from_matrix(data),
                                         gene_names=gene_names)

        self.subsample_genes(new_n_genes=new_n_genes, subset_genes=subset_genes)

    def preprocess(self):
        print("Preprocessing dataset")

        ad = anndata.read_h5ad(self.save_path + self.download_name)
        gene_names = np.array(ad.obs.index.values, dtype=str)
        data = ad.X.T  # change gene * cell to cell * gene
        select = data.sum(axis=1) > 0  # Take out cells that doesn't express any gene
        data = data[select, :]

        print("Finished preprocessing dataset")
        return data, gene_names
