import torch
import torch.nn as nn
import torch.nn.functional as F

from basics.base_module import CategorizedModule
from modules.commons.common_layers import (
    XavierUniformInitLinear as Linear,
)
from modules.diffusion.ddpm import (
    GaussianDiffusion, PitchDiffusion
)
from modules.fastspeech.acoustic_encoder import FastSpeech2Acoustic
from modules.fastspeech.param_adaptor import ParameterAdaptorModule
from modules.fastspeech.tts_modules import LengthRegulator
from modules.fastspeech.variance_encoder import FastSpeech2Variance
from utils.hparams import hparams


class DiffSingerAcoustic(ParameterAdaptorModule, CategorizedModule):
    @property
    def category(self):
        return 'acoustic'

    def __init__(self, vocab_size, out_dims):
        super().__init__()
        self.fs2 = FastSpeech2Acoustic(
            vocab_size=vocab_size
        )

        if self.predict_variances:
            self.variance_adaptor = self.build_adaptor()
            self.variance_embeds = nn.ModuleDict({
                name: Linear(1, hparams['hidden_size'])
                for name in self.variance_prediction_list
            })

        self.diffusion = GaussianDiffusion(
            out_dims=out_dims,
            num_feats=1,
            timesteps=hparams['timesteps'],
            k_step=hparams['K_step'],
            denoiser_type=hparams['diff_decoder_type'],
            denoiser_args=(
                hparams['residual_layers'],
                hparams['residual_channels']
            ),
            spec_min=hparams['spec_min'],
            spec_max=hparams['spec_max']
        )

    def forward(
            self, txt_tokens, mel2ph, f0, key_shift=None, speed=None,
            spk_embed_id=None, gt_mel=None, infer=True, **kwargs
    ):
        adaptor_cond, mel_cond = self.fs2(
            txt_tokens, mel2ph, f0, key_shift=key_shift, speed=speed,
            spk_embed_id=spk_embed_id, infer=infer, **kwargs
        )

        variance_inputs = self.collect_variance_inputs(**kwargs)
        if infer:
            if not self.predict_variances:
                variance_pred_out = {}
            elif not all([v is not None for v in variance_inputs]):
                variance_outputs = self.variance_adaptor(adaptor_cond, variance_inputs, infer)
                variance_choices = [
                    v_in if v_in is not None else v_pred
                    for v_in, v_pred in zip(variance_inputs, variance_outputs)
                ]
                variance_embeds = torch.stack([
                    self.variance_embeds[v_name](v_choice[:, :, None])  # [B, T] => [B, T, H]
                    for v_name, v_choice in zip(self.variance_prediction_list, variance_choices)
                ], dim=-1).sum(-1)
                mel_cond += variance_embeds
                variance_pred_out = self.collect_variance_outputs(variance_choices)
            else:
                variance_pred_out = {
                    name: kwargs[name]
                    for name in self.variance_prediction_list
                }

            mel_pred_out = self.diffusion(mel_cond, infer=True)
            mel_pred_out *= ((mel2ph > 0).float()[:, :, None])

        else:
            if self.predict_variances:
                variance_pred_out = self.variance_adaptor(adaptor_cond, variance_inputs, infer)

                variance_embeds = torch.stack([
                    self.variance_embeds[v_name](v_choice[:, :, None])  # [B, T] => [B, T, H]
                    for v_name, v_choice in zip(self.variance_prediction_list, variance_inputs)
                ], dim=-1).sum(-1)
                mel_cond += variance_embeds
            else:
                variance_pred_out = None

            mel_pred_out = self.diffusion(mel_cond, gt_spec=gt_mel, infer=False)

        return mel_pred_out, variance_pred_out


class DiffSingerVariance(ParameterAdaptorModule, CategorizedModule):
    @property
    def category(self):
        return 'variance'

    def __init__(self, vocab_size):
        super().__init__()
        self.predict_dur = hparams['predict_dur']
        self.fs2 = FastSpeech2Variance(
            vocab_size=vocab_size
        )
        self.lr = LengthRegulator()

        self.predict_pitch = hparams['predict_pitch']

        predict_energy = hparams['predict_energy']
        predict_breathiness = hparams['predict_breathiness']
        self.variance_prediction_list = []
        if predict_energy:
            self.variance_prediction_list.append('energy')
        if predict_breathiness:
            self.variance_prediction_list.append('breathiness')
        self.predict_variances = len(self.variance_prediction_list) > 0

        if self.predict_pitch:
            pitch_hparams = hparams['pitch_prediction_args']
            self.base_pitch_embed = Linear(1, hparams['hidden_size'])
            self.pitch_predictor = PitchDiffusion(
                vmin=pitch_hparams['pitch_delta_vmin'],
                vmax=pitch_hparams['pitch_delta_vmax'],
                repeat_bins=pitch_hparams['num_pitch_bins'],
                timesteps=hparams['timesteps'],
                k_step=hparams['K_step'],
                denoiser_type=hparams['diff_decoder_type'],
                denoiser_args=(
                    pitch_hparams['residual_layers'],
                    pitch_hparams['residual_channels']
                )
            )

        if self.predict_variances:
            self.pitch_embed = Linear(1, hparams['hidden_size'])
            self.variance_predictor = self.build_adaptor()

    def forward(
            self, txt_tokens, midi, ph2word, ph_dur=None, word_dur=None, mel2ph=None,
            base_pitch=None, delta_pitch=None, infer=True, **kwargs
    ):
        encoder_out, dur_pred_out = self.fs2(
            txt_tokens, midi=midi, ph2word=ph2word,
            ph_dur=ph_dur, word_dur=word_dur, infer=infer
        )

        if not self.predict_pitch and not self.predict_variances:
            return dur_pred_out, None, None, ({} if infer else None)

        if mel2ph is None or hparams['dur_cascade']:
            # (extract mel2ph from dur_pred_out)
            raise NotImplementedError()

        encoder_out = F.pad(encoder_out, [0, 0, 1, 0])
        mel2ph_ = mel2ph[..., None].repeat([1, 1, hparams['hidden_size']])
        condition = torch.gather(encoder_out, 1, mel2ph_)

        if self.predict_pitch:
            pitch_cond = condition + self.base_pitch_embed(base_pitch[:, :, None])
            pitch_pred_out = self.pitch_predictor(pitch_cond, delta_pitch, infer)
        else:
            pitch_pred_out = None

        if not self.predict_variances:
            return dur_pred_out, pitch_pred_out, ({} if infer else None)

        if delta_pitch is None:
            pitch = base_pitch + pitch_pred_out
        else:
            pitch = base_pitch + delta_pitch
        pitch_embed = self.pitch_embed(pitch[:, :, None])
        condition += pitch_embed

        variance_inputs = self.collect_variance_inputs(**kwargs)
        variance_outputs = self.variance_predictor(condition, variance_inputs, infer)
        if infer:
            variances_pred_out = self.collect_variance_outputs(variance_outputs)
        else:
            variances_pred_out = variance_outputs

        return dur_pred_out, pitch_pred_out, variances_pred_out
