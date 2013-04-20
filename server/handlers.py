# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser
import json
import jwt
import re
import templeton.handlers
import web

from collections import defaultdict
from datetime import datetime, timedelta
from math import sqrt

import autophonedb


# "/api/" is automatically prepended to each of these
urls = (
    '/s1s2/add/?', 'S1S2RawFennecAddResult',
    '/s1s2/info/?', 'S1S2RawFennecParameters',
    '/s1s2/data/?', 'S1S2RawFennecData',
    '/s1s2/delete/?', 'S1S2RawFennecDeleteResults'
)

config = ConfigParser.ConfigParser()
config.read('settings.cfg')

try:
    REQUIRE_SIGNED = config.getboolean('server', 'require_signed')
except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
    REQUIRE_SIGNED = False

try:
    CLIENT_KEYS = dict(config.items('client keys'))
except ConfigParser.NoSectionError:
    CLIENT_KEYS = {}


def get_mean_stddev(values):
    count = len(values)
    if count == 1:
        return values[0], 0
    mean = sum(values) / float(count)
    stddev = sqrt(sum([(value - mean)**2 for value in values])/float(count-1))
    return mean, stddev


def is_clean(s):
    return bool(re.match('[\w\.\- ]+$', s))


class S1S2RawFennecAddResult():
    @templeton.handlers.json_response
    def POST(self):
        content_type = web.ctx.env.get('CONTENT_TYPE', '')
        if content_type == 'application/jwt':
            token = jwt.decode(web.data(),
                               signers=[jwt.jws.HmacSha(keydict=CLIENT_KEYS)])
            if not token['valid']:
                print 'Bad signature from %s!  Ignoring results.' % \
                    token['headers'].get('kid', '(unknown)')
                raise web.badrequest('bad signature')
            r = token['payload']
        elif REQUIRE_SIGNED:
            print 'Signature required but plain JSON received.  Ignoring ' \
                'results.'
            raise web.badrequest('signature required')
        else:
            r = json.loads(web.data())

        result = {'runstamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

        try:
            result['starttime'] = int(r['data']['starttime'])
            result['throbberstart'] = int(r['data']['throbberstart'])
            result['throbberstop'] = int(r['data']['throbberstop'])
            result['cached'] = int(r['data']['cached'])
            result['blddate'] = datetime.fromtimestamp(
                float(r["data"]["blddate"])).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise web.badrequest()

        for key in ('phoneid', 'testname', 'revision', 'bldtype', 'productname',
                    'productversion', 'osver', 'machineid'):
            if not is_clean(r['data'][key]):
                raise web.badrequest()
            result[key] = r['data'][key]

        autophonedb.db.insert(autophonedb.SQL_TABLE, **result)


class S1S2RawFennecDeleteResults(object):

    def POST(self):
        r = json.loads(web.data())
        try:
            vars = dict(revision=r['revision'],
                        phoneid=r['phoneid'],
                        bldtype=r['bldtype'])
        except KeyError:
            raise web.badrequest()
        autophonedb.db.delete(autophonedb.SQL_TABLE,
                              where='revision=$revision and phoneid=$phoneid '
                              'and bldtype=$bldtype', vars=vars)


class S1S2RawFennecParameters(object):

    @templeton.handlers.json_response
    def GET(self):
        phones = [x['phoneid'] for x in autophonedb.db.query(
            'select distinct phoneid from %s' % autophonedb.SQL_TABLE)]
        tests = [x['testname'] for x in autophonedb.db.query(
            'select distinct testname from %s' % autophonedb.SQL_TABLE)]
        products = [x['productname'] for x in autophonedb.db.query(
            'select distinct productname from %s' % autophonedb.SQL_TABLE)]
        return {'phone': phones, 'test': tests, 'product': products}


class S1S2RawFennecData(object):

    metrics = { 'throbberstart': 'throbberstart',
                'throbberstop': 'throbberstop',
                'totalthrobber': 'throbberstop-throbberstart' }

    @templeton.handlers.json_response
    def GET(self):
        query, body = templeton.handlers.get_request_parms()
        test = query['test'][0]
        start = query['start'][0]
        # add one to the end date so we capture the full end day
        # e.g. if the user gives an end day of 2012-01-01, we want
        # everything on that day, so really we want everything before
        # 2012-01-02.
        end = (datetime.strptime(query['end'][0], '%Y-%m-%d').date() +
               timedelta(days=1)).strftime('%Y-%m-%d')

        metric = query['metric'][0]
        metric_column = self.metrics[metric]
        product = query['product'][0]
        cached = query['cached'][0] == 'cached'
        errorbars = query['errorbars'][0] == 'errorbars'
        initialonly = query['initialonly'][0] == 'initialonly'

        # results[phone][test][metric][blddate] = value
        results = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(dict))))

        data_validity_check = 'throbberstart>0'
        if metric != 'throbberstart':
          data_validity_check += ' and %s>0' % metric_column

        data = autophonedb.db.select(
            autophonedb.SQL_TABLE,
            what=self.metrics[metric] + ',starttime,blddate,revision,phoneid,cached',
            where='testname=$test and productname=$product and '
            'blddate >= $start and blddate < $end and cached=$cached and ' + data_validity_check,
            vars=dict(test=test, product=product, start=start, end=end, cached=cached))

        for d in data:
            blddate = d['blddate']
            if not isinstance(blddate, datetime):
                blddate = datetime.strptime(blddate, '%Y-%m-%d %H:%M:%S')
            r = results[d['phoneid']][test][metric][blddate.isoformat()]
            if metric == 'totalthrobber':
                offsettime = 0
            else:
                offsettime = d['starttime']
            if 'values' in r:
                r['values'].append(d[self.metrics[metric]] - offsettime)
            else:
                r['values'] = [d[self.metrics[metric]] - offsettime]
            if not 'revision' in r:
                r['revision'] = d['revision']
        for d in results.values():
            for r in d[test][metric].values():
                if initialonly or len(r['values']) == 1:
                    r['value'], r['stddev'] = r['values'][0], 0
                else:
                    r['value'], r['stddev'] = get_mean_stddev(r['values'][1:])
                del r['values']
        return results
