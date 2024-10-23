# This file is automatically generated by pyo3_stub_gen
# ruff: noqa: E501, F401

import typing

class VotesAggregator:
    def __new__(cls,): ...
    def flush_votes(self) -> bool:
        ...

    def get_num_votes(self) -> int:
        ...

    @staticmethod
    def validate_raw_vote(rankings: typing.Sequence[int]) -> tuple[bool, str]:
        ...

    def insert_vote_ranking(self, vote_id:int, vote_ranking:int) -> None:
        ...

    def insert_empty_votes(self, num_votes:int) -> bool:
        ...

    def determine_winner(self) -> typing.Optional[int]:
        ...


