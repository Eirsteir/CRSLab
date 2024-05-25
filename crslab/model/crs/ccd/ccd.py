import json
import os
import re

import editdistance
import torch
from transformers import (
    pipeline, AutoModelForCausalLM, AutoTokenizer,
)

from crslab.config import DATASET_PATH
from crslab.model.base import BaseModel

PROMPT_TEMPLATE = (
            "Pretend you are a course curriculum recommender system. "
            "I will give you a conversation between a user and you (a recommender system). "
            "Based on the conversation, you reply me with 50 topic recommendations without extra sentences."
            "\n\nHere is the conversation: \n'{}' "
            "\n\nList of 50 recommendations: "
)


def parse_topics(input_string):
    # Define a regex pattern to extract text after the number and period
    pattern = re.compile(r"\d+\.\s+(.*)")

    # Find all matches in the input string and return them as a list
    topics = pattern.findall(input_string)

    return [topic.lower() for topic in topics]




def compute_edit_distance(str1, str2):
    return editdistance.eval(str1, str2)


def find_similar_topic(input_topic, topics):
    # Find the topic in the list that has the smallest edit distance to the input topic
    min_distance = float("inf")
    closest_topic = None

    for topic in topics:
        distance = compute_edit_distance(input_topic, topic)

        if distance < min_distance:
            min_distance = distance
            closest_topic = topic

    return closest_topic, min_distance


def match_topics(all_items, predicted_items, threshold=2):
    if not all_items:
        return []

    matched_predictions = []

    for item in predicted_items:
        closest_topic, min_distance = find_similar_topic(item, all_items)

        if min_distance <= threshold:
            matched_predictions.append(closest_topic)
        # TODO: We should not add it if it does not match?
        else:
            matched_predictions.append("OOD")

    return matched_predictions


class HuggingfaceModel(BaseModel):
    def __init__(self, opt, device, dpath=None, resource=None):
        dataset = opt['dataset'].lower()
        self.dataset_path = os.path.join(DATASET_PATH, dataset, "none")

        with open(f"{self.dataset_path}/entity2id.json", 'r', encoding="utf-8") as f:
            self.entity2id = json.load(f)

        self.entity2id["OOD"] = -1  # TODO: double check this

        self.id2entity = {idx: entity for entity, idx in self.entity2id.items()}
        self.all_items = list(self.id2entity.values())

        self.model_id = opt["model_id"]
        self.max_target_length = opt["max_target_length"]
        self.torch_dtype = torch.bfloat16 if opt["bf16"] else torch.float32

        print(f"Using {self.torch_dtype=}")

        super().__init__(opt, device)

    def build_model(self):
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_id,
            attn_implementation="sdpa",
            torch_dtype=self.torch_dtype,
        )
        self.model.to(self.device)

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id, use_fast=True)
        self.tokenizer.pad_token = self.tokenizer.eos_token
        self.tokenizer.padding_side = "left"

        self.generation_kwargs = dict(
            pad_token_id=self.tokenizer.eos_token_id,  # Ensure padding is consistent
            max_new_tokens=self.max_target_length,
            do_sample=True,
            temperature=0.6,
            top_p=0.9,
        )

        if self.model_id in [
            "meta-llama/Meta-Llama-3-8B-Instruct",
            "eirsteir/crs-llama-3-8b-instruct",
        ]:
            self.generation_kwargs["eos_token_id"] = [
                self.tokenizer.eos_token_id,
                self.tokenizer.convert_tokens_to_ids("<|eot_id|>"),
            ]

    def recommend(self, batch, mode="test"):
        messages = self._format_context_for_chat_input(batch)
        model_inputs = self._apply_chat_template_and_tokenize(messages)
        input_length = model_inputs.shape[1]

        with torch.no_grad():
            generated_ids = self.model.generate(model_inputs, **self.generation_kwargs)

        responses = self.tokenizer.batch_decode(generated_ids[:, input_length:], skip_special_tokens=True)

        all_predicted_recs = [parse_topics(response) for response in responses]
        all_predicted_recs = [match_topics(all_items=self.all_items, predicted_items=recs) for recs in all_predicted_recs]
        all_predicted_recs = [[self.entity2id[rec] for rec in recs] for recs in all_predicted_recs]

        return all_predicted_recs

    def _format_context_for_chat_input(self, batch):
        prompts = [PROMPT_TEMPLATE.format(context) for context in batch["context"]]
        messages = [[{"role": "user", "content": prompt}] for prompt in prompts]
        return messages

    def converse(self, batch, mode="test"):
        model_inputs = self._apply_chat_template_and_tokenize(batch["context"])
        input_length = model_inputs.shape[1]

        with torch.no_grad():
            generated_ids = self.model.generate(model_inputs, **self.generation_kwargs)

        responses = self.tokenizer.batch_decode(generated_ids[:, input_length:], skip_special_tokens=True)
        return responses

    def _apply_chat_template_and_tokenize(self, messages):
        return self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, padding=True, return_tensors="pt"
        ).to(self.device)

    def guide(self, batch, mode):
        pass