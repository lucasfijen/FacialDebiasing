from enum import Enum
from datasets.h5celeba import H5CelebA
import os
from datasets.h5imagenet import H5Imagenet
import torch
from torch.utils.data import ConcatDataset, DataLoader, Dataset, WeightedRandomSampler, BatchSampler, SequentialSampler
from torch.utils.data.dataset import Subset
from torch.utils.data.sampler import RandomSampler
from setup import config
import h5py
import numpy as np
from typing import Optional, List, NamedTuple, Union
from logger import logger

from datasets.data_utils import CountryEnum, DataLoaderTuple, GenderEnum, SkinColorEnum
from datasets.celeb_a import CelebDataset
from datasets.imagenet import ImagenetDataset
from datasets.ppb import PPBDataset

def split_dataset(dataset, train_size: float, max_images: Optional[int] = None):
    # Shuffle indices of the dataset
    idxs: np.array = np.arange(len(dataset))
    np.random.seed(config.random_seed)
    np.random.shuffle(idxs)

    # Sample sub-selection
    sampled_idxs: np.array = idxs if not max_images else idxs[:min(max_images, len(idxs))]

    # Split dataset
    split: int = int(np.floor(train_size * len(sampled_idxs)))
    train_idxs = sampled_idxs[:split]
    valid_idxs = sampled_idxs[split:]

    # Subsample dataset with given validation indices
    train_data = Subset(dataset, train_idxs)
    valid_data = Subset(dataset, valid_idxs)

    return train_data, valid_data

def concat_datasets(dataset_a, dataset_b, proportion_a: Optional[float] = None):
    if proportion_a:
        proportion_b = 1 - proportion_a
        # Calculate amount of dataset
        nr_dataset_a: int = int(np.floor(proportion_a * len(dataset_a)))
        nr_dataset_b: int = int(np.floor(proportion_b * len(dataset_b)))

    else:
        nr_dataset_a = len(dataset_a)
        nr_dataset_b = len(dataset_b)

    # Subsample the datasets
    sampled_dataset_a = Subset(dataset_a, np.arange(nr_dataset_a))
    sampled_dataset_b = Subset(dataset_b, np.arange(nr_dataset_b))

    return ConcatDataset([sampled_dataset_a, sampled_dataset_b])

def make_h5_datasets(path_to_h5_train: str, **kwargs):
    if not os.path.exists(path_to_h5_train):
        logger.error(
            f"Unable to find h5 file for training file at {path_to_h5_train}",
            next_step="Will stop training / evaluation",
            tip="Double check your path_to_h5_train in the config, and check if you downloaded the appropriate .h5 file"
        )
        raise Exception
    
    with h5py.File(config.path_to_h5_train, mode='r') as h5_file:
        labels = h5_file['labels'][()].flatten()
        files = h5_file['images']

        idxs_faces = np.where(labels > 0)[0].flatten()
        idxs_nonfaces = np.where(labels == 0)[0].flatten()

        files_faces: h5py.Dataset = files[idxs_faces.tolist()]
        files_nonfaces: h5py.Dataset = files[idxs_nonfaces.tolist()]

        dataset_nonfaces: H5Imagenet = H5Imagenet(files_nonfaces, path_to_images='', **kwargs)
        dataset_faces: H5CelebA = H5CelebA(files_faces, path_to_images='', **kwargs)

        return dataset_faces, dataset_nonfaces


def make_train_and_valid_loaders(
    batch_size: int,
    max_images: int,
    shuffle: bool = True,
    train_size: float = 0.8,
    proportion_faces: float = 0.5,
    enable_debias: bool = True,
    sample_bias_with_replacement: bool = True,
    use_h5: bool = True,
    **kwargs
):
    nr_images: Optional[int] = max_images if max_images >= 0 else None

    # Create the datasets
    if use_h5:
        celeb_dataset, imagenet_dataset = make_h5_datasets(**kwargs)
    else:
        imagenet_dataset = ImagenetDataset(path_to_images=config.path_to_imagenet_images, **kwargs)
        celeb_dataset = CelebDataset(path_to_images=config.path_to_celeba_images, path_to_bbox=config.path_to_celeba_bbox_file, **kwargs)

    # Split both datasets into training and validation
    celeb_train, celeb_valid = split_dataset(celeb_dataset, train_size, nr_images)
    imagenet_train, imagenet_valid = split_dataset(imagenet_dataset, train_size, nr_images)

    # Nonfaces loaders
    train_nonfaces_loader: DataLoader = DataLoader(imagenet_train, batch_size=int(batch_size / 2), shuffle=shuffle, num_workers=config.num_workers)
    valid_nonfaces_loader: DataLoader = DataLoader(imagenet_valid, batch_size=int(batch_size / 2), shuffle=False, num_workers=config.num_workers)

    # Init some weights
    init_weights = torch.rand(len(celeb_train)).tolist()

    # Define samplers: random for non-debias, weighed for debiasing
    random_train_sampler = RandomSampler(celeb_train)
    weights_sampler_train = WeightedRandomSampler(init_weights, len(celeb_train), replacement=sample_bias_with_replacement)

    train_sampler = weights_sampler_train if enable_debias else random_train_sampler

    # Define the face loaders
    train_faces_loader: DataLoader = DataLoader(celeb_train, sampler=train_sampler, batch_size=int(batch_size / 2), num_workers=config.num_workers)
    valid_faces_loader: DataLoader = DataLoader(celeb_valid, batch_size=int(batch_size / 2), shuffle=shuffle, num_workers=config.num_workers)

    train_loaders: DataLoaderTuple = DataLoaderTuple(train_faces_loader, train_nonfaces_loader)
    valid_loaders: DataLoaderTuple = DataLoaderTuple(valid_faces_loader, valid_nonfaces_loader)

    return train_loaders, valid_loaders

class EvalDatasetType(Enum):
    PBB_ONLY = 'ppb'
    IMAGENET_ONLY = 'imagenet'
    H5_IMAGENET_ONLY = 'h5_imagenet'

def make_eval_loader(
    filter_exclude_gender: List[str] = [],
    filter_exclude_country: List[str] = [],
    filter_exclude_skin_color: List[str] = [],
    max_images: int = -1,
    proportion_faces: float = 0.5,
    dataset_type: str = EvalDatasetType.PBB_ONLY.value,
    **kwargs
):
    if dataset_type == EvalDatasetType.PBB_ONLY.value:
        logger.info('Evaluating on PPB')

        dataset = PPBDataset(
            path_to_images=config.path_to_eval_face_images,
            path_to_metadata=config.path_to_eval_metadata,
            filter_excl_country=filter_exclude_country,
            filter_excl_gender=filter_exclude_gender,
            filter_excl_skin_color=filter_exclude_skin_color,
            get_sub_images=True,
            **kwargs
        )
    elif dataset_type == EvalDatasetType.IMAGENET_ONLY.value:
        logger.info('Evaluating on Imagenet')

        dataset = ImagenetDataset(
            path_to_images=config.path_to_eval_nonface_images,
            get_sub_images=True
            **kwargs
        )
    else:
        logger.info('Evaluating on Imagenet H5')

        _, h5_nonfaces = make_h5_datasets()

        dataset = H5Imagenet(
            path_to_images='',
            h5_dataset=h5_nonfaces.dataset,
            get_sub_images=True
            **kwargs
        )

    nr_images: Optional[int] = max_images if max_images >= 0 else None

    if nr_images:
        dataset = subsample_dataset(dataset, nr_images, random=True)

    # Concat and wrap with loader
    data_loader = DataLoader(dataset, batch_size=1, shuffle=True, num_workers=config.num_workers)

    return data_loader

def subsample_dataset(dataset: Dataset, nr_subsamples: int, random=False):
    idxs = np.arange(nr_subsamples)

    if random:
        idxs = np.random.choice(np.arange(len(dataset)), nr_subsamples)

    return Subset(dataset, idxs)


def sample_dataset(dataset: Dataset, nr_samples: int):
    max_nr_items: int = min(nr_samples, len(dataset))
    idxs = np.random.permutation(np.arange(len(dataset)))[:max_nr_items]

    return torch.stack([dataset[idx][0] for idx in idxs])

def sample_idxs_from_loaders(idxs, data_loaders, label):
    if label == 1:
        dataset = data_loaders.faces.dataset.dataset
    else:
        dataset = data_loaders.nonfaces.dataset.dataset

    return torch.stack([dataset[idx.item()][0] for idx in idxs])

def sample_idxs_from_loader(idxs, data_loader, label):
    if label == 1:
        dataset = data_loader.dataset.dataset
    else:
        dataset = data_loader.dataset.dataset

    return torch.stack([dataset[idx.item()][0] for idx in idxs])

def make_hist_loader(dataset, batch_size):
    sampler = SequentialSampler(dataset)
    batch_sampler = BatchSampler(sampler, batch_size=batch_size, drop_last=False)

    return DataLoader(dataset, batch_sampler=batch_sampler)
