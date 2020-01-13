#!/usr/bin/python

from __future__ import print_function
import httplib

index = 'inside'
solr_host = 'ol-search-inside:8983'

h1 = httplib.HTTPConnection(solr_host)
h1.connect()
url = 'http://%s/solr/%s/update' % (solr_host, index)
h1.request('POST', url, '<commit />', { 'Content-type': 'text/xml;charset=utf-8'})
response = h1.getresponse()
response_body = response.read()
if response.reason != 'OK':
    print(response.reason)
    print(response_body)
assert response.reason == 'OK'
h1.close()
