import numpy as np
import pandas as pd
import torch
from init_wandb import Wandb_Init
from constants import Directories
from transformers.tokenization_utils_base import PreTrainedTokenizerBase
from torch.optim import Adam
from torch.utils.data import Dataset, DataLoader
from transformers import T5Tokenizer, T5ForConditionalGeneration
from torch import cuda

"""
source:
https://colab.research.google.com/github/abhimishra91/transformers-tutorials/blob/master/transformers_summarization_wandb.ipynb#scrollTo=dMZD0QCwnB6w
"""

device = "cuda" if cuda.is_available() else "cpu"


class CustomDataset(Dataset):
    def __init__(self, dataframe, tokenizer, source_len, summ_len):
        self.tokenizer = tokenizer
        self.data = dataframe
        self.source_len = source_len
        self.summ_len = summ_len
        self.text = self.data.text
        self.ctext = self.data.ctext

    def __len__(self):
        return len(self.text)

    def __getitem__(self, item):
        ctext = str(self.ctext[item])
        ctext = " ".join(ctext.split())

        text = str(self.text[item])
        text = " ".join(text.split())

        source = self.tokenizer.batch_encode_plus(
            [ctext],
            max_length=self.source_len,
            padding="max_length",
            return_tensors="pt",
            truncation=True,
        )
        target = self.tokenizer.batch_encode_plus(
            [text],
            max_length=self.summ_len,
            padding="max_length",
            return_tensors="pt",
            truncation=True,
        )

        source_ids = source.get("input_ids").squeeze()
        source_mask = source.get("attention_mask").squeeze()
        target_ids = target.get("input_ids").squeeze()
        target.get("attention_mask").squeeze()

        return {
            "source_ids": source_ids.to(dtype=torch.long),
            "source_mask": source_mask.to(dtype=torch.long),
            "target_ids": target_ids.to(dtype=torch.long),
            "target_ids_y": target_ids.to(dtype=torch.long),
        }


def train(
    epoch: int,
    tokenizer: PreTrainedTokenizerBase,
    model: tuple,
    device: str,
    loader: DataLoader,
    optimizer: Adam,
    wandb_init,
) -> None:
    model.train()
    for _, data in enumerate(loader, 0):
        y = data.get("target_ids").to(device, dtype=torch.long)
        y_ids = y[:, :-1].contiguous()
        lm_labels = y[:, 1:].clone().detach()
        lm_labels[y[:, 1:] == tokenizer.pad_token_id] = -100
        ids = data.get("source_ids").to(device, dtype=torch.long)
        mask = data.get("source_mask").to(device, dtype=torch.long)

        # https://github.com/priya-dwivedi/Deep-Learning/issues/137
        # lm_labels -> labels
        outputs = model(
            input_ids=ids,
            attention_mask=mask,
            decoder_input_ids=y_ids,
            labels=lm_labels,
        )

        loss = outputs[0]

        if _ % 10 == 0:
            try:
                wandb_init.wandb.log({"Training Loss": loss.item()})
            except RuntimeError:
                pass

        if _ % 500 == 0:
            try:
                print(f"Epoch: {epoch}, Loss: {loss.item()}")
            except RuntimeError:
                pass

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()


def validate(
    epoch: int,
    tokenizer: PreTrainedTokenizerBase,
    model: tuple,
    device: str,
    loader: DataLoader,
) -> tuple[list[str], list[str]]:
    model.eval()
    predictions = []
    actuals = []
    with torch.no_grad():
        for _, data in enumerate(loader, 0):
            y = data["target_ids"].to(device, dtype=torch.long)
            ids = data["source_ids"].to(device, dtype=torch.long)
            mask = data["source_mask"].to(device, dtype=torch.long)

            generated_ids = model.generate(
                input_ids=ids,
                attention_mask=mask,
                max_length=150,
                num_beams=2,
                repetition_penalty=2.5,
                length_penalty=1.0,
                early_stopping=True,
            )
            preds = [
                tokenizer.decode(
                    g, skip_special_tokens=True, clean_up_tokenization_spaces=True
                )
                for g in generated_ids
            ]
            target = [
                tokenizer.decode(
                    t, skip_special_tokens=True, clean_up_tokenization_spaces=True
                )
                for t in y
            ]
            if _ % 100 == 0:
                print(f"Completed {_}")

            predictions.extend(preds)
            actuals.extend(target)
    return predictions, actuals


def read_training_dataset():
    df = pd.read_csv(
        Directories.TRAIN_SETS.joinpath("news_summary.csv"), encoding="latin-1"
    )
    df = df[["text", "ctext"]]
    df.ctext = "summarize: " + df.ctext
    print(df.head())
    return df


def main(which_llm: str, model_output: str, train_epoch: int = 2):
    # WandB – Initialize a new run
    wandb_init = Wandb_Init(model_output, train_epoch)

    # Set random seeds and deterministic pytorch for reproducibility
    torch.manual_seed(wandb_init._config.SEED)  # pytorch random seed
    np.random.seed(wandb_init._config.SEED)  # numpy random seed
    torch.backends.cudnn.deterministic = True

    # tokenzier for encoding the text
    tokenizer: PreTrainedTokenizerBase = T5Tokenizer.from_pretrained(
        Directories.LLM_DIR.joinpath(which_llm)
    )
    df = read_training_dataset()

    # Creation of Dataset and Dataloader
    # Defining the train size. So 80% of the data will be used for training and the rest will be used for validation.
    train_size = 0.8
    train_dataset = df.sample(frac=train_size, random_state=wandb_init._config.SEED)
    val_dataset = df.drop(train_dataset.index).reset_index(drop=True)
    train_dataset = train_dataset.reset_index(drop=True)

    print("FULL Dataset: {}".format(df.shape))
    print("TRAIN Dataset: {}".format(train_dataset.shape))
    print("TEST Dataset: {}".format(val_dataset.shape))

    # Creating the Training and Validation dataset for further creation of Dataloader
    training_set = CustomDataset(
        train_dataset,
        tokenizer,
        wandb_init._config.MAX_LEN,
        wandb_init._config.SUMMARY_LEN,
    )
    val_set = CustomDataset(
        val_dataset,
        tokenizer,
        wandb_init._config.MAX_LEN,
        wandb_init._config.SUMMARY_LEN,
    )

    # Defining the parameters for creation of dataloaders
    train_params = {
        "batch_size": wandb_init._config.TRAIN_BATCH_SIZE,
        "shuffle": True,
        "num_workers": 0,
    }

    val_params = {
        "batch_size": wandb_init._config.VALID_BATCH_SIZE,
        "shuffle": False,
        "num_workers": 0,
    }

    # Creation of Dataloaders for testing and validation. This will be used down for training and validation stage for the model.
    training_loader = DataLoader(training_set, **train_params)
    val_loader = DataLoader(val_set, **val_params)

    # Defining the model. We are using t5-base model and added a Language model layer on top for generation of Summary.
    # Further this model is sent to device (GPU/TPU) for using the hardware.
    model: tuple = T5ForConditionalGeneration.from_pretrained(
        Directories.LLM_DIR.joinpath(which_llm)
    )
    model = model.to(device)

    # Defining the optimizer that will be used to tune the weights of the network in the training session.
    optimizer: Adam = torch.optim.Adam(
        params=model.parameters(), lr=wandb_init._config.LEARNING_RATE
    )

    # Log metrics with wandb
    wandb_init.wandb.watch(model, log="all")
    # Training loop
    print("Initiating Fine-Tuning for the model on our dataset")

    for epoch in range(wandb_init._config.TRAIN_EPOCHS):
        train(epoch, tokenizer, model, device, training_loader, optimizer, wandb_init)
    model.save_pretrained(
        Directories.TRAINED_MODEL_DIR.joinpath(model_output)
    )  # save model into trained_model/
    # Validation loop and saving the resulting file with predictions and acutals in a dataframe.
    # Saving the dataframe as predictions.csv
    print(
        "Now generating summaries on our fine tuned model for the validation dataset and saving it in a dataframe"
    )
    for epoch in range(wandb_init._config.VAL_EPOCHS):
        predictions, actuals = validate(epoch, tokenizer, model, device, val_loader)
        final_df = pd.DataFrame({"Generated Text": predictions, "Actual Text": actuals})
        final_df.to_csv("./models/predictions.csv")
        print("Output Files generated for review")


if __name__ == "__main__":
    main("t5-small", train_epoch=2, model_output="trained-model-t5-small-test1")
