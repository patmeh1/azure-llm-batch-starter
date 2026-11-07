import pytest
import tiktoken

from llm_batch_starter.config import Settings
from llm_batch_starter.prepare import estimate_input_tokens, synthetic_target_padding
from llm_batch_starter.synthetic import generate_dataset


@pytest.mark.parametrize("tokens", [1000, 74572, 110000])
def test_repeated_reference_reencodes_to_the_requested_size(tokens):
    record = generate_dataset(1, reference_tokens=tokens)[0]
    reference = record.text.partition("REFERENCE\n")[2].rsplit("\nREQUEST\n", 1)[0]
    assert len(tiktoken.get_encoding("o200k_base").encode(reference)) == tokens


@pytest.mark.parametrize("target", [1000, 74572, 110000])
def test_calibrated_request_target_matches_the_actual_payload(target):
    settings = Settings()
    padding = synthetic_target_padding(target, settings)
    record = generate_dataset(1, reference_tokens=padding)[0]
    assert abs(estimate_input_tokens(record, settings) - target) <= 2
