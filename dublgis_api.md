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
