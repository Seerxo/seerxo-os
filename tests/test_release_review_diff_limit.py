"""The review packet size cap (MAX_REVIEW_DIFF) is a hard boundary: the signer answers 400 for anything larger, so a
release whose diff exceeds it can never get a signed review (gateway install 2026-10-22, finding B-9). Needs no
ssh-keygen, so it also runs inside the gateway's offline checks container."""
from app import release_gateway as rg

BASE, SHA, NONCE = "1" * 40, "2" * 40, "ab" * 16


def packet(size: int) -> dict:
    return rg.review_packet("seerxo/os", BASE, SHA, "x" * size, NONCE)


def test_a_diff_exactly_at_the_cap_is_a_valid_packet():
    assert rg.valid_packet(packet(rg.MAX_REVIEW_DIFF))


def test_one_character_over_the_cap_is_not_a_valid_packet():
    assert not rg.valid_packet(packet(rg.MAX_REVIEW_DIFF + 1))


def test_the_cap_is_one_and_a_half_million_characters():
    # Pinned: raising it changes what the signer forwards to the reviewer model; lowering it blocks more releases.
    assert rg.MAX_REVIEW_DIFF == 1_500_000
