from dataclasses import dataclass
from typing import Protocol, Sequence, TypeVar

ResultT = TypeVar("ResultT", contravariant=True)


@dataclass(frozen=True, slots=True)
class PageData:
    url: str
    title: str
    html: str
    markdown: str


@dataclass(frozen=True, slots=True)
class SiteScrapeConfig:
    max_pages: int = 40
    page_concurrency: int = 1
    verbose: bool = False


class WriteRepository(Protocol[ResultT]):
    def add_many(self, items: Sequence[ResultT]) -> None:
        ...
