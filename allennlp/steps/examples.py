import copy
import dataclasses
import logging
import re
from typing import (
    Set,
    Optional,
    Union,
    List,
    Dict,
    Any,
    Tuple,
    Iterable,
)

import datasets
import torch.optim
from torch import cuda
from datasets import Dataset

from allennlp.common import cached_transformers, Lazy, Tqdm
from allennlp.common.checks import check_for_gpu
from allennlp.common.util import log_frozen_and_tunable_parameter_names, sanitize
from allennlp.data import (
    DataLoader,
    Vocabulary,
    Instance,
    TensorDict,
    Field,
)
from allennlp.data.fields import ListField, IndexField
from allennlp.data.fields.transformer_text_field import TransformerTextField
from allennlp.models import Model
from allennlp.nn.util import move_to_device
from allennlp.steps.dataloader import (
    TangoDataLoader,
    MaxBatchesDataLoader,
    BatchSizeDataLoader,
    DataLoaderAdapter,
)
from allennlp.steps.dataset import AllenNlpDataset
from allennlp.steps.format import TorchFormat
from allennlp.steps.step import Step
from allennlp.training import Checkpointer, TrainerCallback, GradientDescentTrainer
from allennlp.training.learning_rate_schedulers import LearningRateScheduler
from allennlp.training.momentum_schedulers import MomentumScheduler
from allennlp.training.moving_average import MovingAverage
from allennlp.training.optimizers import Optimizer


logger = logging.getLogger(__name__)


@Step.register("hf_dataset")
class HuggingfaceDataset(Step):
    DETERMINISTIC = True
    VERSION = "001"
    CACHEABLE = False  # These are already cached by huggingface.

    def run(self, dataset_name: str) -> AllenNlpDataset:
        return AllenNlpDataset(datasets.load_dataset(dataset_name), None, {"source": "huggingface"})


@Step.register("text_only")
class TextOnlyDataset(Step):
    DETERMINISTIC = True

    def run(self, input: AllenNlpDataset, fields_to_keep: Set[str]) -> AllenNlpDataset:
        return dataclasses.replace(
            input,
            splits={
                split_name: [
                    {"text": field_value}
                    for instance in split
                    for field_name, field_value in instance.items()
                    if field_name in fields_to_keep
                ]
                for split_name, split in input.splits.items()
            },
        )


@Step.register("hf_tokenizer")
class Tokenize(Step):
    """This step converts strings in the original dataset into `TransformerTextField`s."""

    DETERMINISTIC = True
    VERSION = "001"
    CACHEABLE = True

    def run(
        self,
        tokenizer_name: str,
        input: AllenNlpDataset,
        fields_to_tokenize: Optional[List[str]] = None,
        add_special_tokens: bool = True,
        max_length: Optional[int] = 512,
        special_tokens_mask: bool = False,
        offset_mapping: bool = False,
    ) -> AllenNlpDataset:
        tokenizer = cached_transformers.get_tokenizer(tokenizer_name)
        assert tokenizer.pad_token_type_id == 0

        field_names_used = set()

        # find all the strings
        if fields_to_tokenize is None:

            def should_tokenize_field(_: str) -> bool:
                return True

        else:
            regexes_to_tokenize = [re.compile(r) for r in fields_to_tokenize]

            def should_tokenize_field(field_name: str) -> bool:
                for r in regexes_to_tokenize:
                    if r.fullmatch(field_name):
                        return True
                return False

        def find_string_objects(o: Any, prefix: str = "") -> Iterable[Tuple[str, str]]:
            prefix = prefix.lstrip(".")
            if isinstance(o, str):
                if should_tokenize_field(prefix):
                    yield prefix, o
            elif isinstance(o, List):
                for i, item in enumerate(o):
                    yield from find_string_objects(item, f"{prefix}.{i}")
            elif isinstance(o, Dict):
                for name, item in o.items():
                    yield from find_string_objects(item, f"{prefix}.{name}")

        strings = []
        for split_name, instances in input.splits.items():
            for instance in instances:
                for name, string in find_string_objects(instance):
                    field_names_used.add(name)
                    strings.append(string)

        for field_name in sorted(field_names_used):
            logging.info("Tokenizing field %s", field_name)

        # This thing is so complicated because we want to call `batch_encode_plus` with all
        # the strings at once.
        encoded = tokenizer.batch_encode_plus(
            strings,
            add_special_tokens=add_special_tokens,
            truncation=max_length is not None,
            max_length=max_length,
            return_token_type_ids=True,
            return_attention_mask=False,
            return_special_tokens_mask=special_tokens_mask,
            return_offsets_mapping=offset_mapping,
        )

        # make fields
        string_to_field = {
            s: TransformerTextField(
                torch.tensor(encoded["input_ids"][i], dtype=torch.int32),
                torch.tensor(encoded["token_type_ids"][i], dtype=torch.int32),
                torch.tensor(encoded["attention_mask"][i], dtype=torch.bool)
                if "attention_mask" in encoded
                else None,
                torch.tensor(encoded["special_tokens_mask"][i], dtype=torch.bool)
                if "special_tokens_mask" in encoded
                else None,
                torch.tensor(encoded["offset_mapping"][i], dtype=torch.int32)
                if "offset_mapping" in encoded
                else None,
                tokenizer.pad_token_id,
            )
            for i, s in enumerate(strings)
        }

        def replace_string_objects(o: Any) -> Any:
            if isinstance(o, str):
                try:
                    return string_to_field[o]
                except KeyError:
                    return o
            elif isinstance(o, List) or isinstance(o, Dataset):
                return [replace_string_objects(i) for i in o]
            elif isinstance(o, Dict):
                return {key: replace_string_objects(value) for key, value in o.items()}
            else:
                return o

        new_splits = {
            split_name: replace_string_objects(split_data)
            for split_name, split_data in input.splits.items()
        }

        # make vocab
        if input.vocab is not None:
            vocab = copy.deepcopy(input.vocab)
        else:
            vocab = Vocabulary.empty()

        for name in field_names_used:
            vocab.add_transformer_vocab(tokenizer, name)

        return AllenNlpDataset(new_splits, vocab)


@Step.register("piqa_instances")
class PiqaInstances(Step):
    DETERMINISTIC = True
    VERSION = "002"
    CACHEABLE = True

    def run(
        self,
        tokenizer_name: str,
        max_length: int = 512,
    ) -> AllenNlpDataset:
        tokenizer = cached_transformers.get_tokenizer(tokenizer_name)
        assert tokenizer.pad_token_type_id == 0

        dataset = {
            split_name: [
                {
                    "correct_alternative": instance["label"],
                    "alternatives": [
                        (instance["goal"], instance["sol1"]),
                        (instance["goal"], instance["sol2"]),
                    ],
                }
                for instance in instances
            ]
            for split_name, instances in datasets.load_dataset("piqa").items()
        }

        # This thing is so complicated because we want to call `batch_encode_plus` with all
        # the strings at once.
        tokenized = {
            split_name: tokenizer.batch_encode_plus(
                [alternative for instance in instances for alternative in instance["alternatives"]],
                add_special_tokens=True,
                truncation=True,
                max_length=max_length,
                return_token_type_ids=True,
                return_attention_mask=False,
            )
            for split_name, instances in dataset.items()
        }

        result = {}
        for split_name, instances in dataset.items():
            tokenized_alts = tokenized[split_name]
            results_per_split = []
            for i, instance in enumerate(instances):
                alts = ListField(
                    [
                        TransformerTextField(
                            torch.tensor(tokenized_alts["input_ids"][alt_index], dtype=torch.int32),
                            torch.tensor(
                                tokenized_alts["token_type_ids"][alt_index], dtype=torch.int32
                            ),
                        )
                        for alt_index in [2 * i, 2 * i + 1]
                    ]
                )
                fields: Dict[str, Field] = {"alternatives": alts}
                if instance["correct_alternative"] >= 0:
                    fields["correct_alternative"] = IndexField(
                        instance["correct_alternative"], alts
                    )
                results_per_split.append(Instance(fields))
            result[split_name] = results_per_split

        # make vocab
        vocab = Vocabulary.empty()
        vocab.add_transformer_vocab(tokenizer, "tokens")

        return AllenNlpDataset(result, vocab)


