"""
Code originally from Google's Chirp project implementation of NOTELA:
https://github.com/google-research/chirp/blob/main/chirp/projects/sfda/methods/notela.py
"""
import logging
import copy
import numpy as np
from scipy import sparse
import torch

from pyha_analyzer import pseudolabel, config, utils
from pyha_analyzer.models.timm_model import TimmModel
from pyha_analyzer.train import TrainProcess

cfg = config.cfg
logger = logging.getLogger("acoustic_multiclass_training")


def cdist(
        features_a: np.ndarray, 
        features_b: np.ndarray
    ) -> np.ndarray: 
    """
    A numpy equivalent of scipy.spatial.distance.cdist. 
    Computes the pairwise squared euclidean distance between 
    each pair of features from features_a and features_b. 
    Args: 
        features_a: The first batch of features, 
            expected shape [*, batch_size_a, feature_dim] 
        features_b: The second batch of features, 
            expected shape [*, batch_size_b, feature_dim] 
    Returns: 
        The pairwise squared euclidean distance between each 
        pair of features from features_a and features_b. 
        Shape [*, batch_size_a, batch_size_b] 
    Raises: ValueError: If the shape of features_a's last dimension 
    does not match the shape of feature_b's last dimension. 
    """ 
    if features_a.shape[-1] != features_b.shape[-1]: 
        raise ValueError( 
                "The feature dimension should be the same. Currently features_a: " 
                f"{features_a.shape} and features_b: {features_b.shape}" 
        ) 
    feature_dim = features_a.shape[-1] 
    flat_features_a = np.reshape(features_a, [-1, feature_dim]) 
    flat_features_b = np.reshape(features_b, [-1, feature_dim]) 
    flat_transpose_b = flat_features_b.T 
    distances = ( 
            np.sum(np.square(flat_features_a), 1, keepdims=True) 
            - 2 * np.matmul(flat_features_a, flat_transpose_b) 
            + np.sum(np.square(flat_transpose_b), 0, keepdims=True) 
    ) 
    return distances

def compute_nearest_neighbors(
        batch_feature: np.ndarray,
        dataset_feature: np.ndarray,
        knn: int,
        memory_efficient_computation: bool = True
    ) -> sparse.csr_matrix:
    """
    Compute batch_feature's nearest-neighbors among dataset_feature.
    
        Args:
            batch_feature: The features for the provided batch of data, 
                shape [batch_size, feature_dim]
            dataset_feature: The features for the whole dataset, 
                shape [dataset_size, feature_dim]
            knn: The number of nearest-neighbors to use.
            memory_efficient_computation: Whether to make computation memory
                efficient. This option trades speed for memory footprint by looping over
                samples in the batch instead of fully vectorizing nearest-neighbor
                computation. For large datasets, memory usage can be a bottleneck, which
                is why we set this option to True by default.
            
        Returns:
            The batch's nearest-neighbors affinity matrix of shape [batch_size, dataset_size], 
            where position (i, j) indicates whether dataset_feature[j] 
            belongs to batch_feature[i]'s nearest-neighbors.
            
        Raises:
            ValueError: If batch_feature and dataset_feature don't have the same
            number of dimensions, or if their feature dimension don't match.
    """
    assert isinstance( batch_feature, np.ndarray)
    assert isinstance( dataset_feature, np.ndarray)
    batch_shape = batch_feature.shape
    dataset_shape = dataset_feature.shape

    if batch_feature.ndim != dataset_feature.ndim or (
        batch_shape[-1] != dataset_shape[-1]
    ):

        raise ValueError(
            "Batch features and dataset features' shapes are not consistent."
            f"(batch_feature: {batch_shape} and dataset_feature: {dataset_shape})"
        )

    neighbors = min(dataset_shape[0], knn)
    if memory_efficient_computation:
        # We loop over samples in the current batch to avoid storing a
        # batch_size x dataset_size float array. That slows down computation, but
        # reduces memory footprint, which becomes the bottleneck for large
        # datasets.
        col_indices = []
        i = 0
        for sample_feature in batch_feature:
            i +=1
            pairwise_distances = cdist(
                np.expand_dims(sample_feature, 0), dataset_feature
            )  # [1, dataset_size]
            col_indices.append(
                torch.topk(
                    torch.tensor(-pairwise_distances), 
                    torch.tensor(neighbors) #type: ignore 
                )[1][:, 1:] 
            )  
                #[1, neighbors-1] 
            assert int(col_indices[-1].shape[0])==1
        col_indices = torch.stack(col_indices).numpy() #(23, *)
    else:
        pairwise_distances = cdist(
            batch_feature, dataset_feature
        )  # [batch_size, dataset_size]
        col_indices = torch.topk(-pairwise_distances, neighbors)[1][ #type: ignore 
            :, 1:
        ]  # [batch_size, neighbors-1]
    col_indices = col_indices.flatten()  # [batch_size * neighbors-1]
    row_indices = np.repeat(
        np.arange(batch_shape[0]), neighbors - 1
    )  # [0, ..., 0, 1, ...]
    nn_matrix = np.zeros((batch_shape[0], dataset_shape[0]), dtype=np.uint8) 
    #[batch_size, dataset_size]

    data = np.ones(row_indices.shape[0])
    nn_matrix = sparse.csr_matrix(
        (data, (row_indices, col_indices)),
        shape=(batch_shape[0], dataset_shape[0]),
    )
    return nn_matrix

def teacher_step(
        batch_prob: np.ndarray,
        dataset_prob: np.ndarray,
        nn_matrix: sparse.csr_matrix,
        lambda_: float,
        alpha: float = 1.0,
        normalize_pseudo_labels: bool = True,
        eps: float = 1e-8
    ) -> np.ndarray:
    """Computes the pseudo-labels (teacher-step) following Eq.(3) in the paper.

    Args:
      batch_proba: The model's probabilities on the current batch of data.
        Expected shape [batch_size, proba_dim]
      dataset_proba: The model's probabilities on the rest of the dataset.
        Expected shape [dataset_size, proba_dim]
      nn_matrix: The affinity between the points in the current batch
        (associated to `batch_proba`) and the remaining of the points
        (associated to `dataset_proba`), of shape [batch_size, dataset_size].
        Specifically, position [i,j] informs if point j belongs to i's
        nearest-neighbors.
      lambda_: Weight controlling the Laplacian regularization.
      alpha: Weight controlling the Softness regularization
      normalize_pseudo_labels: Whether to normalize pseudo-labels to turn them
        into valid probability distributions. This option should be kept to
        True, and only be used for experimental purposes.
      eps: For numerical stability.

    Returns:
      The soft pseudo-labels for the current batch of data, shape
        [batch_size, proba_dim]
    """
    # By default, sum operation on a csr_matrix keeps the dimensions of the
    # original matrix.
    denominator = nn_matrix.sum(axis=-1)

    # In the limit where alpha goes to zero, we can rewrite the expression as
    #
    #     pseudo_label = [batch_proba * jnp.exp(lambda_ * ...)] ** (1 / alpha)
    #
    # and see that the normalized pseudo-label probabilities take value 1 if
    # they have the maximum value for the expression above over the class axis
    # and zero otherwise.
    if alpha == 0 and normalize_pseudo_labels:
        pseudo_label = batch_prob * np.exp(
            lambda_
            * (nn_matrix @ dataset_prob)
            / (denominator + eps)  # [*, batch_size, proba_dim]
        )
        pseudo_label = (
            pseudo_label == pseudo_label.max(axis=-1, keepdims=True)
        ).astype(np.float32)
        # If more than one class is maximally probable, we need to renormalize the
        # distribution to be uniform over the maximally-probable classes.
        pseudo_label /= np.asarray(pseudo_label).sum(axis=-1, keepdims=True)
    else:
        pseudo_label = np.multiply((batch_prob ** (1 / alpha)), np.exp(
            (lambda_ / alpha) * (nn_matrix @ dataset_prob) / (denominator + eps)
        ))  # [*, batch_size, proba_dim]
        if normalize_pseudo_labels:
            assert isinstance(pseudo_label, np.ndarray)
            pseudo_label /= np.asarray(pseudo_label).sum(axis=-1, keepdims=True) + eps
    return pseudo_label

def get_dataset_info(model, dl):
    """Get indices, images, predictions, and features from dataset"""
    indices = []
    data = []
    predictions = []
    features = []
    for (mels, _, index) in dl:
        assert all(float(idx).is_integer() for idx in index)
        indices.append(index)
        data.append(mels)
        predictions.append(torch.sigmoid(model(mels.cuda()).cpu()))
        features.append(model.get_features(mels.cuda()).cpu())
    
    #indices = torch.cat(indices, dim=0)
    #data = torch.cat(data, dim=0)
    #features = torch.cat(features, dim=0)
    #predictions = torch.cat(predictions, dim=0)
    return [torch.cat(lst, dim=0) for lst in (indices, data, predictions, features)]
    #return indices, data, predictions, features

#def apply_thresholding(predictions): #[dataset_size, num_classes]
#    threshold = cfg.pseudo_threshold
#    pseudo_labels = np.vectorize(lambda x: 1 if x > threshold else 0)(predictions)
#    return torch.tensor(pseudo_labels)

def one_hot_to_name(annotation: np.ndarray, class_to_idx):
    """Convert one hot annotation to species name"""
    if 1 not in annotation: 
        return None
    # class_to_idx should be bijective
    annotation_to_name = {v: k for k, v in class_to_idx.items()} 
    name = annotation_to_name[int(np.argmax(annotation))]
    return name

#TODO: Convert to multilabel?
def one_hot(prediction: np.ndarray) -> np.ndarray:
    """Convert prediction to one hot annotation"""
    #TODO: What happens if you have empty annotation?
    one_hot_annotation = np.zeros(prediction.shape)
    max_val = np.max(prediction, axis=1)
    max_idx = np.argmax(prediction, axis=1)
    if max_val>cfg.pseudo_threshold:
        one_hot_annotation[0, max_idx] = 1
    return one_hot_annotation
    

def get_names(pseudolabels, class_to_idx):
    """Get pseudolabels species names"""
    pseudolabels = [one_hot(pseudolabel) for pseudolabel in pseudolabels]
    pseudolabels = [one_hot_to_name(annotation, class_to_idx) for annotation in pseudolabels]
    return pseudolabels


def get_regularized_pseudolabels(features, predictions):
    """Get pseudolabels regularized by feature distances"""
    nn_matrix = compute_nearest_neighbors(
            features.detach().numpy(), copy.deepcopy(features.detach().numpy()), knn=cfg.notela_knn
    ) # [dataset_size, dataset_size]
    regularized_pseudolabels = teacher_step(
            predictions.detach().numpy(), 
            copy.deepcopy(predictions.detach().numpy()),
            nn_matrix,
            lambda_ = cfg.notela_lambda
    ) # [dataset_size, num_classes]
    return regularized_pseudolabels 

def update_dataset_predictions(pseudo_labels, indices, train_process):
    """Add new predictions to dataset"""
    (train_process
            .train_dl
            .dataset
            .samples[cfg.manual_id_col]
            .iloc[indices]
    ) = pseudo_labels



def finetune(model):
    """Fine tune on pseudo labels"""
    _, train_dl, valid_dl, infer_dl = pseudolabel.pseudo_label_data(model)

    logger.info("Finetuning on pseudo labels...")
    train_process = TrainProcess(model, train_dl, valid_dl, infer_dl)
    utils.wandb_init(in_sweep = False, project_suffix = "nutella")
    #train_process.valid()
    #train_process.inference_valid()
    for _ in range(cfg.epochs):
        indices, _, predictions, features = get_dataset_info(model, train_dl)
        assert torch.all(predictions>0)
        pseudolabels = get_regularized_pseudolabels(features, predictions)
        class_to_idx = train_process.train_dl.dataset.class_to_idx #type: ignore
        pseudolabels = get_names(pseudolabels, class_to_idx)
        update_dataset_predictions(
                train_process = train_process, 
                indices = indices,
                pseudo_labels = pseudolabels
        )
        train_process.run_epoch()
        train_process.valid()
        train_process.inference_valid()

def main():
    model = TimmModel(len(cfg.class_list), model_name=cfg.model).to(cfg.device)
    finetune(model)

if __name__=="__main__":
    main()
