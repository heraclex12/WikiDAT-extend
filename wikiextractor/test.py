from elasticsearch import Elasticsearch
from wikiextractor.clean import clean_markup
if __name__ == '__main__':
    es = Elasticsearch(['10.30.78.22'])
    data = es.get(index='viwiki_history', id=16907537)['_source']

    print(clean_markup(data['content']))
