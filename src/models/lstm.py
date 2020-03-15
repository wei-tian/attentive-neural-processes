import os
import numpy as np
import pandas as pd
import torch
from tqdm.auto import tqdm
from torch import nn
from torch.nn import functional as F
from torch.utils.data import DataLoader
from torchvision.datasets import MNIST
from test_tube import Experiment, HyperOptArgumentParser
import torchvision.transforms as transforms
from argparse import ArgumentParser
import json
import pytorch_lightning as pl
from matplotlib import pyplot as plt
import torch
import io
import PIL
from torchvision.transforms import ToTensor

from src.data.smart_meter import get_smartmeter_df

from src.utils import ObjectDict


class SequenceDfDataSet(torch.utils.data.Dataset):
    def __init__(self, df, hparams, label_names=None, train=True, transforms=None):
        super().__init__()
        self.data = df
        self.hparams = hparams
        self.label_names = label_names
        self.train = train
        self.transforms = transforms

    def __len__(self):
        return len(self.data) - self.hparams.window_length - self.hparams.target_length - 1

    def iloc(self, idx):
        k = idx + self.hparams.window_length + self.hparams.target_length
        j = k - self.hparams.target_length
        i = j - self.hparams.window_length
        assert i >= 0
        assert idx <= len(self.data)

        x_rows = self.data.iloc[i:k].copy()
        # x_rows = x_rows.drop(columns=self.label_names)
        # Note the NP models do have access to the previous labels for the context, we will allow the LSTM to do the same. Although it will likely just return an autoregressive solution for the first half...
        x_rows.loc[x_rows.index[self.hparams.window_length:], self.label_names] = 0
        assert len(x_rows.loc[x_rows.index[self.hparams.window_length:], self.label_names])>0
        assert (x_rows.loc[x_rows.index[self.hparams.window_length:], self.label_names]==0).all().all()

        y_rows = self.data[self.label_names].iloc[i+1:k+1].copy()
        #         print(i,j,k)

        # add seconds since start of window index
        x_rows["tstp"] = (
            x_rows["tstp"] - x_rows["tstp"].iloc[0]
        ).dt.total_seconds() / 86400.0
        return x_rows, y_rows

    def __getitem__(self, idx):
        x_rows, y_rows = self.iloc(idx)

        x = x_rows.astype(np.float32).values
        y = y_rows[self.label_names].astype(np.float32).values
        return (
            self.transforms(x).squeeze(0).float(),
            self.transforms(y).squeeze(0).squeeze(-1).float(),
        )


class LSTMNet(nn.Module):
    def __init__(self, hparams):
        super().__init__()
        self.hparams = hparams

        self.lstm1 = nn.LSTM(
            input_size=self.hparams.input_size,
            hidden_size=self.hparams.hidden_size,
            batch_first=True,
            num_layers=self.hparams.lstm_layers,
            bidirectional=self.hparams.bidirectional,
            dropout=self.hparams.lstm_dropout,
        )
        self.hidden_out_size = (
            self.hparams.hidden_size
            * (self.hparams.bidirectional + 1)
        )
        self.linear = nn.Linear(self.hidden_out_size, 1)

    def forward(self, x):
        outputs, (h_out, _) = self.lstm1(x)
        # outputs: [B, T, num_direction * H]
        y = self.linear(outputs).squeeze(2)
        return y


class LSTM_PL(pl.LightningModule):
    def __init__(self, hparams):
        # TODO make label name configurable
        # TODO make data source configurable
        super().__init__()
        self.hparams = ObjectDict()
        self.hparams.update(
            hparams.__dict__ if hasattr(hparams, "__dict__") else hparams
        )
        self._model = LSTMNet(self.hparams)
        self._dfs = None

    def forward(self, x):
        return self._model(x)

    def training_step(self, batch, batch_idx):
        # REQUIRED
        x, y = batch
        y_hat = self.forward(x)
        y = y[:, self.hparams.window_length:]
        y_hat = y_hat[:, self.hparams.window_length:]
        loss = F.mse_loss(y_hat, y)
        tensorboard_logs = {"train_loss": loss}
        return {"loss": loss, "log": tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        x, y = batch
        y_hat = self.forward(x)
        y = y[:, self.hparams.window_length:]
        y_hat = y_hat[:, self.hparams.window_length:]
        loss = F.mse_loss(y_hat, y)
        tensorboard_logs = {"val_loss": loss}
        return {"val_loss": loss, "log": tensorboard_logs}

    def validation_end(self, outputs):
        # TODO send an image to tensroboard, like in the lighting_anp.py file
        if int(self.hparams["vis_i"]) > 0:
            loader = self.val_dataloader()
            vis_i = min(int(self.hparams["vis_i"]), len(loader.dataset))
        if isinstance(self.hparams["vis_i"], str):
            image = plot_from_loader(loader, self, vis_i=vis_i, window_len=self.hparams["window_length"])
            plt.show()
        else:
            image = plot_from_loader_to_tensor(loader, self, vis_i=vis_i, window_len=self.hparams["window_length"])
            self.logger.experiment.add_image(
                "val/image", image, self.trainer.global_step
            )

        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        keys = outputs[0]["log"].keys()
        tensorboard_logs = {
            k: torch.stack([x["log"][k] for x in outputs if k in x["log"]]).mean()
            for k in keys
        }
        tensorboard_logs_str = {k: f"{v}" for k, v in tensorboard_logs.items()}
        print(f"step {self.trainer.global_step}, {tensorboard_logs_str}")
        assert torch.isfinite(avg_loss)
        return {"avg_val_loss": avg_loss, "log": tensorboard_logs}

    def test_step(self, *args, **kwargs):
        return self.validation_step(*args, **kwargs)

    def test_end(self, *args, **kwargs):
        return self.validation_end(*args, **kwargs)

    def configure_optimizers(self):
        optim = torch.optim.Adam(self.parameters(), lr=self.hparams["learning_rate"])
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optim, patience=self.hparams["patience"], verbose=True, min_lr=1e-5
        )  # note early stopping has patient 3
        return [optim], [scheduler]

    def _get_cache_dfs(self):
        if self._dfs is None:
            df_train, df_val, df_test = get_smartmeter_df()
            self._dfs = dict(df_train=df_train, df_val=df_val, df_test=df_test)
        return self._dfs

    @pl.data_loader
    def train_dataloader(self):
        df_train = self._get_cache_dfs()["df_train"]
        dset_train = SequenceDfDataSet(
            df_train,
            self.hparams,
            label_names=["energy(kWh/hh)"],
            transforms=transforms.ToTensor(),
            train=True,
        )
        return DataLoader(
            dset_train,
            batch_size=self.hparams.batch_size,
            shuffle=True,
            num_workers=self.hparams.num_workers,
        )

    @pl.data_loader
    def val_dataloader(self):
        df_test = self._get_cache_dfs()["df_val"]
        dset_test = SequenceDfDataSet(
            df_test,
            self.hparams,
            label_names=["energy(kWh/hh)"],
            train=False,
            transforms=transforms.ToTensor(),
        )
        return DataLoader(dset_test, batch_size=self.hparams.batch_size, shuffle=False)

    @pl.data_loader
    def test_dataloader(self):
        df_test = self._get_cache_dfs()["df_test"]
        dset_test = SequenceDfDataSet(
            df_test,
            self.hparams,
            label_names=["energy(kWh/hh)"],
            train=False,
            transforms=transforms.ToTensor(),
        )
        return DataLoader(dset_test, batch_size=self.hparams.batch_size, shuffle=False)

    @staticmethod
    def add_suggest(trial: optuna.Trial):
        """
        Add hyperparam ranges to an optuna trial and typical user attrs.
        
        Usage:
            trial = optuna.trial.FixedTrial(
                params={         
                    'hidden_size': 128,
                }
            )
            trial = add_suggest(trial)
            trainer = pl.Trainer()
            model = LSTM_PL(dict(**trial.params, **trial.user_attrs), dataset_train,
                            dataset_test, cache_base_path, norm)
            trainer.fit(model)
        """
        trial.suggest_loguniform("learning_rate", 1e-6, 1e-2)
        trial.suggest_uniform("lstm_dropout", 0, 0.75)
        trial.suggest_categorical(
            "hidden_size", [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
        )
        trial.suggest_categorical("lstm_layers", [1, 2, 3, 4, 6,  8])
        trial.suggest_categorical("bidirectional", [False, True])

        trial._user_attrs = {
            "batch_size": 16,
            "grad_clip": 40,
            "max_nb_epochs": 200,
            "num_workers": 4,
            "vis_i": 670,
            "input_size": 6,
            "output_size": 1,
            "patience": 2,
        }
        return trial


def plot_from_loader(loader, model, vis_i=670, n=1, window_len=0):
    dset_test = loader.dataset
    label_names = dset_test.label_names
    y_trues = []
    y_preds = []
    vis_i = min(vis_i, len(dset_test))
    for i in tqdm(range(vis_i, vis_i + n)):
        x_rows, y_rows = dset_test.iloc(i)
        x, y = dset_test[i]
        device = next(model.parameters()).device
        x = x[None, :].to(device)
        model.eval()
        with torch.no_grad():
            y_hat = model.forward(x)
            y_hat = y_hat.cpu().squeeze(0).numpy()

        dt = y_rows.iloc[0].name

        y_hat_rows = y_rows.copy()
        y_hat_rows[label_names[0]] = y_hat
        y_trues.append(y_rows)
        y_preds.append(y_hat_rows)

    plt.figure()
    pd.concat(y_trues)[label_names[0]].plot(label="y_true")
    ylims = plt.ylim()
    pd.concat(y_preds)[label_names[0]][window_len:].plot(label="y_pred")
    plt.legend()
    t_ahead = pd.Timedelta("30T") * model.hparams.target_length
    plt.title(f"predicting {t_ahead} ahead")
    plt.ylim(*ylims)
    # plt.show()


def plot_from_loader_to_tensor(*args, **kwargs):
    plot_from_loader(*args, **kwargs)

    # Send fig to tensorboard
    buf = io.BytesIO()
    plt.savefig(buf, format="jpeg")
    plt.close()
    buf.seek(0)
    image = PIL.Image.open(buf)
    image = ToTensor()(image)  # .unsqueeze(0)
    return image
