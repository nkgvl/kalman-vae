import logging
import os
import tempfile
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
import wandb
from matplotlib.colors import LinearSegmentedColormap
from moviepy.editor import ImageSequenceClip
from tqdm import tqdm

from kvae.kalman_vae import KalmanVariationalAutoencoder
from kvae.sample_control import SampleControl

logger = logging.getLogger(__name__)


def create_continuous_mask(seq_length, mask_length, batch_size, device, dtype):
    lst = [1.0] * seq_length
    start_index = (seq_length - mask_length) // 2
    for i in range(start_index, start_index + mask_length):
        lst[i] = 0.0
    return (
        torch.tensor(lst)
        .repeat(batch_size, 1)
        .transpose(0, 1)
        .to(device=device, dtype=dtype)
    )


def create_random_mask(seq_length, batch_size, mask_rate, device, dtype):
    mask = (torch.rand((seq_length, batch_size), device=device) >= mask_rate).to(
        device=device, dtype=dtype
    )
    mask[0] = 1
    mask[-1] = 1

    return mask


def evaluate(
    dataloader: torch.utils.data.DataLoader,
    kvae: KalmanVariationalAutoencoder,
    sample_control: SampleControl,
    checkpoint_dir: str,
    epoch: int,
    dtype: torch.dtype,
    device: torch.device,
    num_videos: int = 3,
    use_wandb: bool = True,
    show_progress: bool = False,
):
    logger.info("Evaluating model on random masking...")
    random_masking = evaluate_random_masking(
        dataloader=dataloader,
        kvae=kvae,
        sample_control=sample_control,
        dtype=dtype,
        device=device,
    )
    logger.info("Evaluating model on continuous masking...")
    continuous_masking = evaluate_continuous_masking(
        dataloader=dataloader,
        kvae=kvae,
        sample_control=sample_control,
        dtype=dtype,
        device=device,
    )
    table_directory = os.path.join(checkpoint_dir, "tables", f"epoch_{epoch}")
    os.makedirs(table_directory, exist_ok=True)
    random_masking.to_csv(os.path.join(table_directory, "random_masking.csv"))
    continuous_masking.to_csv(os.path.join(table_directory, "continuous_masking.csv"))

    logger.info("Logging videos...")
    log_continuous_masking_video(
        dataloader=dataloader,
        kvae=kvae,
        sample_control=sample_control,
        dtype=dtype,
        device=device,
        video_directory=os.path.join(checkpoint_dir, "videos", f"epoch_{epoch}"),
        metadata={"epoch": epoch},
        num_videos=num_videos,
        use_wandb=use_wandb,
        show_progress=show_progress,
    )

    return random_masking, continuous_masking


def evaluate_random_masking(
    dataloader: torch.utils.data.DataLoader,
    kvae: KalmanVariationalAutoencoder,
    sample_control: SampleControl,
    dtype: torch.dtype,
    device: torch.device,
):
    dropout_probabilities = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1]
    filtering_incorrect_pixels = []
    smoothing_incorrect_pixels = []
    for dropout_probability in dropout_probabilities:
        batch = next(iter(dataloader))
        batch = (batch > 0.5).to(dtype=dtype, device=device)
        seq_length, batch_size, image_channels, *image_size = batch.shape
        mask = create_random_mask(
            seq_length=seq_length,
            batch_size=batch_size,
            mask_rate=dropout_probability,
            device=batch.device,
            dtype=batch.dtype,
        )
        _, info = kvae.elbo(
            xs=batch,
            observation_mask=mask,
            sample_control=sample_control,
        )
        filtered_images = kvae.decoder(info["filter_as"].view(-1, 2)).mean.view(
            seq_length, batch_size, image_channels, *image_size
        )
        filtering_incorrect_pixels.append(
            calculate_fraction_of_incorrect_pixels(batch, filtered_images, mask)
            .cpu()
            .detach()
            .numpy()
            .tolist()
        )
        smoothed_images = kvae.decoder(info["as_resampled"].view(-1, 2)).mean.view(
            seq_length, batch_size, image_channels, *image_size
        )
        smoothing_incorrect_pixels.append(
            calculate_fraction_of_incorrect_pixels(batch, smoothed_images, mask)
            .cpu()
            .detach()
            .numpy()
            .tolist()
        )
    return pd.DataFrame(
        {
            "batch_id": [0] * len(dropout_probabilities),
            "dropout_probabilities": dropout_probabilities,
            "filtering_incorrect_pixels": filtering_incorrect_pixels,
            "smoothing_incorrect_pixels": smoothing_incorrect_pixels,
        }
    )


def evaluate_continuous_masking(
    dataloader: torch.utils.data.DataLoader,
    kvae: KalmanVariationalAutoencoder,
    sample_control: SampleControl,
    dtype: torch.dtype,
    device: torch.device,
) -> pd.DataFrame:
    batch = next(iter(dataloader))
    batch = (batch > 0.5).to(dtype=dtype, device=device)
    seq_length, batch_size, image_channels, *image_size = batch.shape

    mask_lengths = np.arange(2, seq_length - 4, 2).tolist()

    filtering_incorrect_pixels = []
    smoothing_incorrect_pixels = []

    for mask_length in mask_lengths:
        mask = create_continuous_mask(
            seq_length=seq_length,
            mask_length=mask_length,
            batch_size=batch_size,
            device=batch.device,
            dtype=batch.dtype,
        )
        _, info = kvae.elbo(
            xs=batch,
            observation_mask=mask,
            sample_control=sample_control,
        )
        filtered_images = kvae.decoder(info["filter_as"].view(-1, 2)).mean.view(
            seq_length, batch_size, image_channels, *image_size
        )
        filtering_incorrect_pixels.append(
            calculate_fraction_of_incorrect_pixels(batch, filtered_images, mask)
            .cpu()
            .detach()
            .numpy()
            .tolist()
        )
        smoothed_images = kvae.decoder(info["as_resampled"].view(-1, 2)).mean.view(
            seq_length, batch_size, image_channels, *image_size
        )
        smoothing_incorrect_pixels.append(
            calculate_fraction_of_incorrect_pixels(batch, smoothed_images, mask)
            .cpu()
            .detach()
            .numpy()
            .tolist()
        )

    return pd.DataFrame(
        {
            "batch_id": 0,
            "mask_lengths": mask_lengths,
            "filtering_incorrect_pixels": filtering_incorrect_pixels,
            "smoothing_incorrect_pixels": smoothing_incorrect_pixels,
        }
    )


def log_continuous_masking_video(
    dataloader: torch.utils.data.DataLoader,
    kvae: KalmanVariationalAutoencoder,
    sample_control: SampleControl,
    video_directory: str,
    dtype: torch.dtype,
    device: torch.device,
    use_wandb: bool,
    num_videos: int,
    metadata: Optional[dict] = None,
    show_progress: bool = False,
):
    batch = next(iter(dataloader))
    batch = (batch > 0.5).to(dtype=dtype, device=device)
    seq_length, batch_size, image_channels, *image_size = batch.shape

    mask_lengths = [10, 20, 30, 40]
    if show_progress:
        mask_lengths = tqdm(mask_lengths)
    for mask_length in mask_lengths:
        mask = create_continuous_mask(
            seq_length=seq_length,
            mask_length=mask_length,
            batch_size=batch_size,
            device=batch.device,
            dtype=batch.dtype,
        )
        video_count = 0
        for data_idx in range(batch_size):
            if video_count >= num_videos:
                break
            video_path = os.path.join(
                video_directory, f"idx_{data_idx}_mask_length_{mask_length}.mp4"
            )
            write_trajectory_video(
                data=batch[:, data_idx : data_idx + 1],
                kvae=kvae,
                observation_mask=mask[:, data_idx : data_idx + 1],
                filename=video_path,
                channel=0,
                fps=10,
                sample_control=sample_control,
            )
            video_count += 1
            if use_wandb:
                video = wandb.Video(
                    video_path,
                    f"idx_{data_idx}_mask_length_{mask_length}",
                    fps=10,
                    format="mp4",
                )
                log = {
                    "video": video,
                    "batch_id": 0,
                    "data_idx": data_idx,
                    "mask_length": mask_length,
                }
                if metadata is not None:
                    log.update(metadata)
                wandb.log(log)


def calculate_fraction_of_incorrect_pixels(
    image: torch.Tensor,
    reconstructed_image: torch.Tensor,
    observation_mask: torch.Tensor,
):
    incorrect = image != (reconstructed_image > 0.5)
    observation_mask = (
        observation_mask.unsqueeze(-1)
        .unsqueeze(-1)
        .unsqueeze(-1)
        .repeat(1, 1, 1, 16, 16)
        .to(dtype=image.dtype, device=image.device)
    )
    incorrect = incorrect * (1.0 - observation_mask)
    return incorrect.sum() / (1.0 - observation_mask).sum()


def write_trajectory_video(
    data: torch.Tensor,
    kvae: KalmanVariationalAutoencoder,
    observation_mask: torch.Tensor,
    sample_control: SampleControl,
    filename: str,
    channel: int = 0,
    fps: int = 10,
):
    kvae.eval()
    _, info = kvae.elbo(
        xs=data,
        observation_mask=observation_mask,
        sample_control=sample_control,
    )

    seq_length, batch_size, image_channels, *image_size = data.shape
    filtered_images = (
        kvae.decoder(info["filter_as"].view(-1, 2))
        .mean.view(seq_length, batch_size, image_channels, *image_size)
        .cpu()
        .float()
        .detach()
        .numpy()
    )
    smoothed_images = (
        kvae.decoder(info["as_resampled"].view(-1, 2))
        .mean.view(seq_length, batch_size, image_channels, *image_size)
        .cpu()
        .float()
        .detach()
        .numpy()
    )

    idx = 0
    cmap = plt.get_cmap("tab10")
    frame_size = (1600, 400)
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    frame_paths = []
    with tempfile.TemporaryDirectory() as tmpdirname:
        for step, (image) in enumerate((data)):
            fig, axes = plt.subplots(
                figsize=(frame_size[0] / 100, frame_size[1] / 100), nrows=1, ncols=4
            )
            fig.suptitle(f"$t = {step}$")

            image = (image > 0.5).cpu().float().detach().numpy()
            red_grad = LinearSegmentedColormap.from_list(
                "red_grad", [(1, 1, 1), (1, 0, 0)], N=256
            )
            black_grad = LinearSegmentedColormap.from_list(
                "black_grad", [(1, 1, 1), (0, 0, 0)], N=256
            )

            axes[0].imshow(
                image[idx][channel],
                vmin=0,
                vmax=1,
                cmap=red_grad,
                aspect="equal",
                alpha=0.5,
            )
            axes[0].imshow(
                filtered_images[step, idx, 0],
                vmin=0,
                vmax=1,
                cmap=black_grad,
                aspect="equal",
                alpha=0.5,
            )

            axes[1].imshow(
                image[idx][0], vmin=0, vmax=1, cmap=red_grad, aspect="equal", alpha=0.5
            )
            axes[1].imshow(
                smoothed_images[step, idx, 0],
                vmin=0,
                vmax=1,
                cmap=black_grad,
                aspect="equal",
                alpha=0.5,
            )

            axes[2].plot(
                info["as"][:, idx, 0].cpu().detach().numpy(),
                info["as"][:, idx, 1].cpu().detach().numpy(),
                ".-",
                color=cmap(0),
                label="Encoded",
            )

            axes[2].plot(
                info["filter_as"][:, idx, 0].cpu().detach().numpy(),
                info["filter_as"][:, idx, 1].cpu().detach().numpy(),
                ".-",
                color=cmap(1),
                label="Filtered",
            )

            axes[2].plot(
                info["as_resampled"][:, idx, 0].cpu().detach().numpy(),
                info["as_resampled"][:, idx, 1].cpu().detach().numpy(),
                ".-",
                color=cmap(2),
                label="Smoothed",
            )

            for key in ("as", "filter_as", "as_resampled"):
                axes[2].plot(
                    info[key][step, idx, 0].cpu().detach().numpy(),
                    info[key][step, idx, 1].cpu().detach().numpy(),
                    "o",
                    markersize=8,
                    color="red",
                    linestyle="none",
                    zorder=10,
                )

            as_values = info["as"].cpu().detach().numpy()
            mask_values = observation_mask.cpu().detach().numpy()

            selected_x = as_values[:, idx, 0][mask_values[:, idx] == 1]
            selected_y = as_values[:, idx, 1][mask_values[:, idx] == 1]

            axes[2].plot(selected_x, selected_y, "s", color="black", label="Observed")

            axes[3].bar(
                [str(i) for i in range(kvae.state_space_model.K)],
                info["weights"][step, idx].cpu().detach().numpy(),
            )
            axes[3].set_ylim(0, 1)

            axes[0].set_title("from filtered $\\mathbf{z}$")
            axes[1].set_title("from smoothed $\\mathbf{z}$")
            axes[2].set_title("$\\mathbf{a}$ space")
            axes[3].set_title("Mixture weights")
            axes[2].legend()
            axes[2].grid()

            plt.tight_layout()
            frame_path = os.path.join(tmpdirname, f"frame_{step:04d}.png")
            fig.savefig(frame_path, dpi=100)
            frame_paths.append(frame_path)
            plt.close(fig)

        video_clip = ImageSequenceClip(frame_paths, fps=fps)
        video_clip.write_videofile(filename, codec="libx264")
