from typing import Literal

import torch
import torch.distributions as D
import torch.nn as nn
import torch.nn.functional as F

from sample_control import SampleControl
from state_space_model import StateSpaceModel
from variationa_autoencoder import BernoulliDecoder, Encoder, GaussianDecoder


class KalmanVariationalAutoencoder(nn.Module):
    def __init__(
        self,
        image_size,
        image_channels,
        a_dim,
        z_dim,
        K,
        decoder_type: Literal["gaussian", "bernoulli"] = "gaussian",
    ):
        super(KalmanVariationalAutoencoder, self).__init__()
        self.encoder = Encoder(image_size, image_channels, a_dim)
        if decoder_type == "gaussian":
            self.decoder = GaussianDecoder(a_dim, image_size, image_channels)
        elif decoder_type == "bernoulli":
            self.decoder = BernoulliDecoder(a_dim, image_size, image_channels)
        else:
            raise ValueError("Unknown decoder type: {}".format(decoder_type))
        self.state_space_model = StateSpaceModel(a_dim=a_dim, z_dim=z_dim, K=K)
        self.a_dim = a_dim
        self.z_dim = z_dim
        self.register_buffer("_zero_val", torch.tensor(0.0))

    def get_distribution_params(self, xs, symmetrize_covariance=True):
        """Returns the parameters of the distribution over the latent variables"""

        seq_length = xs.shape[0]
        batch_size = xs.shape[1]

        as_distrib = self.encoder(xs.reshape(-1, *xs.shape[2:]))
        as_sample = as_distrib.rsample().view(seq_length, batch_size, self.a_dim)

        # Kalman filter and smoother
        (
            filter_means,
            filter_covariances,
            filter_next_means,
            filter_next_covariances,
            mat_As,
            mat_Cs,
        ) = self.state_space_model.kalman_filter(
            as_sample,
            learn_weight_model=False,
            symmetrize_covariance=symmetrize_covariance,
        )
        means, covariances = self.state_space_model.kalman_smooth(
            as_sample,
            filter_means=filter_means,
            filter_covariances=filter_covariances,
            filter_next_means=filter_next_means,
            filter_next_covariances=filter_next_covariances,
            mat_As=mat_As,
            mat_Cs=mat_Cs,
            symmetrize_covariance=symmetrize_covariance,
        )

        return {
            "filter_means": filter_means,
            "filter_covariances": filter_covariances,
            "filter_next_means": filter_next_means,
            "filter_next_covariances": filter_next_covariances,
            "mat_As": mat_As,
            "mat_Cs": mat_Cs,
            "means": means,
            "covariances": covariances,
        }

    def elbo(
        self,
        xs,
        sample_control: SampleControl,
        observation_mask=None,
        reconst_weight=0.3,
        regularization_weight=1.0,
        kalman_weight=1.0,
        kl_weight=0.0,
        learn_weight_model=True,
        symmetrize_covariance=True,
        burn_in=0,
    ):
        seq_length = xs.shape[0]
        batch_size = xs.shape[1]

        as_distrib = self.encoder(xs.reshape(-1, *xs.shape[2:]))
        if sample_control.encoder == "sample":
            as_ = as_distrib.rsample().view(seq_length, batch_size, self.a_dim)
        elif sample_control.encoder == "mean":
            if self.training:
                raise ValueError(
                    "Invalid sample control for encoder: {}".format(
                        sample_control.encoder
                    )
                )
            as_ = as_distrib.mean.view(seq_length, batch_size, self.a_dim)
        else:
            raise ValueError(
                "Invalid sample control for encoder: {}".format(sample_control.encoder)
            )

        # Reconstruction objective
        xs_distrib = self.decoder(as_.view(-1, self.a_dim))
        reconstruction_obj = (
            xs_distrib.log_prob(xs.reshape(-1, *xs.shape[2:])).view(seq_length, batch_size, *xs.shape[2:]).sum(0).mean(0).sum()
        )

        # Regularization objective
        # -ln q_\phi(a|x)
        regularization_obj = (
            -as_distrib.log_prob(as_.view(-1, self.a_dim)).view(seq_length, batch_size, self.a_dim).sum(0).mean(0).sum()
        )

        # Kalman filter and smoother
        (
            filter_means,
            filter_covariances,
            filter_next_means,
            filter_next_covariances,
            mat_As,
            mat_Cs,
            filter_as,
        ) = self.state_space_model.kalman_filter(
            as_,
            sample_control=sample_control,
            observation_mask=observation_mask,
            learn_weight_model=learn_weight_model,
            symmetrize_covariance=symmetrize_covariance,
            burn_in=burn_in,
        )
        means, covariances, zs, as_resampled = self.state_space_model.kalman_smooth(
            as_,
            filter_means=filter_means,
            filter_covariances=filter_covariances,
            filter_next_means=filter_next_means,
            filter_next_covariances=filter_next_covariances,
            mat_As=mat_As,
            mat_Cs=mat_Cs,
            sample_control=sample_control,
            symmetrize_covariance=symmetrize_covariance,
            burn_in=burn_in,
        )

        # Sample from p_\gamma (z|a,u)
        # Shape of means: (sequence_length, batch_size, z_dim, 1)
        # Shape of covariances: (sequence_length, batch_size, z_dim, z_dim)
        zs_distrib = D.MultivariateNormal(
            means.view(seq_length, batch_size, self.z_dim), covariances.view(seq_length, batch_size, self.z_dim, self.z_dim)
        )

        # KL divergence between q_\phi(a|x) and p(z) for VAE validation purposes
        if kl_weight != 0.0:
            prior_distrib = D.Normal(torch.zeros(self.a_dim), torch.ones(self.a_dim))
            kl_reg = (
                -torch.distributions.kl.kl_divergence(as_distrib, prior_distrib)
                .sum(0)
                .mean(0)
                .sum(0)
            )
        else:
            kl_reg = self._zero_val

        # For testing purposes
        # zs_distrib = D.MultivariateNormal(torch.stack(filter_means).view(-1, self.z_dim), torch.stack(filter_covariances).view(-1, self.z_dim, self.z_dim))

        zs_sample = zs_distrib.rsample()

        # ln p_\gamma(a|z)
        kalman_observation_distrib = D.MultivariateNormal(
            (mat_Cs[:-1] @ zs_sample).view(-1, self.a_dim), self.state_space_model.mat_R
        )
        kalman_observation_log_likelihood = (
            kalman_observation_distrib.log_prob(as_.view(-1, self.a_dim))
            .view(seq_length, batch_size, -1)
            .sum(0)
            .mean(0)
            .sum()
        )

        # ln p_\gamma(z)
        kalman_state_transition_log_likelihood = (
            self.state_space_model.state_transition_log_likelihood(zs_sample, mat_As)
        )

        # ln p_\gamma(z|a)
        kalman_posterior_log_likelihood = (
            zs_distrib.log_prob(zs_sample.view(-1, self.z_dim))
            .view(seq_length, batch_size, -1)
            .sum(0)
            .mean(0)
            .sum()
        )

        objective = (
            reconst_weight * reconstruction_obj
            + regularization_weight * regularization_obj
            + kl_weight * kl_reg
            + kalman_weight
            * (
                kalman_observation_log_likelihood
                + kalman_state_transition_log_likelihood
                - kalman_posterior_log_likelihood
            )
        )

        return objective, {
            "reconst_weight": reconst_weight,
            "regularization_weight": regularization_weight,
            "kalman_weight": kalman_weight,
            "kl_weight": kl_weight,
            "reconstruction": reconstruction_obj.cpu().detach().numpy(),
            "regularization": regularization_obj.cpu().detach().numpy(),
            "kl": kl_reg.cpu().detach().numpy(),
            "kalman_observation_log_likelihood": kalman_observation_log_likelihood.cpu()
            .detach()
            .numpy(),
            "kalman_state_transition_log_likelihood": kalman_state_transition_log_likelihood.cpu()
            .detach()
            .numpy(),
            "kalman_posterior_log_likelihood": kalman_posterior_log_likelihood.cpu()
            .detach()
            .numpy(),
            "observation_mask": observation_mask,
            "filter_means": filter_means,
            "filter_covariances": filter_covariances,
            "filter_next_means": filter_next_means,
            "filter_next_covariances": filter_next_covariances,
            "mat_As": mat_As,
            "mat_Cs": mat_Cs,
            "means": means,
            "covariances": covariances,
            "as": as_,
            "zs": zs,
            "filter_as": filter_as,
            "as_resampled": as_resampled,
        }

    def predict_future(
        self,
        xs,
        num_steps,
        sample_control: SampleControl,
        symmetrize_covariance=True,
    ):
        # TODO: It's too dirty to feed the same data to the encoder

        seq_length = xs.shape[0]
        batch_size = xs.shape[1]

        as_distrib = self.encoder(xs.reshape(-1, *xs.shape[2:]))

        if sample_control.encoder == "sample":
            as_ = as_distrib.sample().view(seq_length, batch_size, self.a_dim)
        elif sample_control.encoder == "mean":
            as_ = as_distrib.mean.view(seq_length, batch_size, self.a_dim)
        else:
            raise ValueError(
                "Invalid sample control for encoder: {}".format(sample_control.encoder)
            )

        # Kalman filter and smoother
        (
            filter_means,
            filter_covariances,
            filter_next_means,
            filter_next_covariances,
            mat_As,
            mat_Cs,
            as_filter,
        ) = self.state_space_model.kalman_filter(
            as_,
            sample_control=sample_control,
            learn_weight_model=False,
            symmetrize_covariance=symmetrize_covariance,
        )
        means, covariances, zs, as_resampled = self.state_space_model.kalman_smooth(
            as_,
            filter_means=filter_means,
            filter_covariances=filter_covariances,
            filter_next_means=filter_next_means,
            filter_next_covariances=filter_next_covariances,
            mat_As=mat_As,
            mat_Cs=mat_Cs,
            symmetrize_covariance=symmetrize_covariance,
            sample_control=sample_control,
        )

        (
            as_,
            means,
            covariances,
            filter_next_means,
            filter_next_covariances,
            mat_As,
            mat_Cs,
        ) = self.state_space_model.predict_future(
            as_,
            means,
            covariances,
            filter_next_means,
            filter_next_covariances,
            mat_As,
            mat_Cs,
            num_steps,
            sample_control=sample_control,
        )

        # shape of as_: (sequence_length + num_steps, batch_size, a_dim, 1)
        xs_distrib = self.decoder(as_.view(-1, self.a_dim))

        if sample_control.decoder == "sample":
            xs = xs_distrib.sample()
        elif sample_control.decoder == "mean":
            xs = xs_distrib.mean
        else:
            raise ValueError(
                "Invalid sample control for decoder: {}".format(sample_control.decoder)
            )

        xs = xs.view(seq_length + num_steps, batch_size, *xs.shape[1:])

        return xs, {
            "as": as_,
            "means": means,
            "covariances": covariances,
            "filter_means": filter_means,
            "filter_covariances": filter_covariances,
            "filter_next_means": filter_next_means,
            "filter_next_covariances": filter_next_covariances,
            "mat_As": mat_As,
            "mat_Cs": mat_Cs,
            "as_filter": as_filter,
            "as_resampled": as_resampled,
        }
