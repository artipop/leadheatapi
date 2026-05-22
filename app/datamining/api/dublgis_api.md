### Ключи

По тестовому ключу
- получаем список рубрик
`curl "https://catalog.api.2gis.com/2.0/catalog/rubric/search?q=стоматология&region_id=32&key=TEST_KEY"`

- список регионов
`curl "https://catalog.api.2gis.com/2.0/region/search?q=Москва&key=TEST_KEY"`

По прод ключу данные по компании:
`curl "https://catalog.api.2gis.com/3.0/items?rubric_id=222,112852&region_id=32&key=${MYKEY}"`
+ `&search_type=one_branch`
+ `&page_size=10&page=1`


nsk 1
kransnoyarsk 7
ekat 9
msk 32
spb 38
omsk 2
kazan 21

rubrics

stomatologiya
222,112852

detailing
110301

beauty saloons
305,5603

wellness
psychologicheskaya pomosch???

sport
110427 Батутные центры
267 Тренажёрные залы
268 Фитнес-клубы
20228 Центры йоги
261 Бассейны
110332 Аквааэробика

### Контракты

- `fetch_items_page(rubric_ids: list[int], region_id: int, api_key: str, page: int, page_size: int, search_type: str)`
- `search_regions(query: str, api_key: str, page: int | None = None, page_size: int | None = None)`
- `search_rubrics(query: str, region_id: int, api_key: str, page: int | None = None, page_size: int | None = None)`
