import sys
import argparse

from habr_parse import fetch_raw_habr_pages, divide_titles_at_weeks
from habr_parse import get_titles_articles_with_raw_habr_pages


def parse_argv():
    description_programm = '''Приложение для подсчёта наиболее употребимых \
существительных в заголовках статей на habr.com'''
    parser = argparse.ArgumentParser(description=description_programm)
    parser.add_argument(
        "--pages", type=int, default=20,
        help="Количество страниц с habr.com. По-умолчанию 20."
    )
    parser.add_argument(
        "--top-size", type=int, default=20,
        help="Количество слов за каждую неделю. По-умолчанию 20."
    )
    return parser.parse_args()


# run it `python habr_example.py --pages 20`
# or `uv run python app/datamining/platforms/habr_top_nouns.py --pages 20`
def main(args):
    args = parse_argv()

    pages_count = args.pages
    top_size = args.top_size

    habr_pages = fetch_raw_habr_pages(pages_count)
    print('получили {0} страниц с habr.com'.format(len(habr_pages)))

    titles_articles = get_titles_articles_with_raw_habr_pages(habr_pages)
    print('количество статей: {0}'.format(len(titles_articles)))

    _ = divide_titles_at_weeks(titles_articles)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
