# Copyright 2016-2019 The Van Valen Lab at the California Institute of
# Technology (Caltech), with support from the Paul Allen Family Foundation,
# Google, & National Institutes of Health (NIH) under Grant U24CA224309-01.
# All rights reserved.
#
# Licensed under a modified Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.github.com/vanvalenlab/kiosk-autoscaler/LICENSE
#
# The Work provided may be used for non-commercial academic purposes only.
# For any other use of the Work, including commercial use, please contact:
# vanvalenlab@gmail.com
#
# Neither the name of Caltech nor the names of its contributors may be used
# to endorse or promote products derived from this software without specific
# prior written permission.
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Tests for the Autoscaler class"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import redis
import pytest

import autoscaler


class Bunch(object):
    def __init__(self, **kwds):
        self.__dict__.update(kwds)


class DummyRedis(object):  # pylint: disable=useless-object-inheritance

    def __init__(self, prefix='predict', status='new', fail_tolerance=0):
        self.prefix = '/'.join(x for x in prefix.split('/') if x)
        self.status = status
        self.fail_count = 0
        self.fail_tolerance = fail_tolerance

    def keys(self):
        if self.fail_count < self.fail_tolerance:
            self.fail_count += 1
            raise redis.exceptions.ConnectionError('thrown on purpose')
        return [
            '{}_{}_{}'.format(self.prefix, self.status, 'x.tiff'),
            '{}_{}_{}'.format(self.prefix, 'failed', 'x.zip'),
            '{}_{}_{}'.format('train', self.status, 'x.TIFF'),
            '{}_{}_{}'.format(self.prefix, self.status, 'x.ZIP'),
            '{}_{}_{}'.format(self.prefix, 'done', 'x.tiff'),
            '{}_{}_{}'.format('train', self.status, 'x.zip'),
        ]

    def scan_iter(self, match=None):
        if self.fail_count < self.fail_tolerance:
            self.fail_count += 1
            raise redis.exceptions.ConnectionError('thrown on purpose')

        keys = [
            '{}_{}_{}'.format(self.prefix, self.status, 'x.tiff'),
            '{}_{}_{}'.format(self.prefix, 'failed', 'x.zip'),
            '{}_{}_{}'.format('train', self.status, 'x.TIFF'),
            '{}_{}_{}'.format(self.prefix, self.status, 'x.ZIP'),
            '{}_{}_{}'.format(self.prefix, 'done', 'x.tiff'),
            '{}_{}_{}'.format('train', self.status, 'x.zip'),
            'malformedKey'
        ]
        if match:
            return (k for k in keys if k.startswith(match[:-1]))
        return (k for k in keys)

    def expected_keys(self, suffix=None):
        for k in self.keys():
            v = k.split('_')
            if v[0] == self.prefix:
                if v[1] == self.status:
                    if suffix:
                        if v[-1].lower().endswith(suffix):
                            yield k
                    else:
                        yield k

    def hget(self, rhash, field):
        if self.fail_count < self.fail_tolerance:
            self.fail_count += 1
            raise redis.exceptions.ConnectionError('thrown on purpose')
        if field == 'status':
            return rhash.split('_')[1]
        elif field == 'file_name':
            return rhash.split('_')[-1]
        elif field == 'input_file_name':
            return rhash.split('_')[-1]
        elif field == 'output_file_name':
            return rhash.split('_')[-1]
        return False


class DummyKubernetes(object):

    def list_namespaced_deployment(self, *args, **kwargs):
        return Bunch(items=[
            Bunch(spec=Bunch(replicas='4'), metadata=Bunch(name='pod1')),
            Bunch(spec=Bunch(replicas='8'), metadata=Bunch(name='pod2'))
        ])

    def list_namespaced_job(self, *args, **kwargs):
        return Bunch(items=[
            Bunch(spec=Bunch(completions='1'), metadata=Bunch(name='pod1')),
            Bunch(spec=Bunch(completions='2'), metadata=Bunch(name='pod2'))
        ])

    def patch_namespaced_deployment(self, *args, **kwargs):
        return Bunch(items=[Bunch(spec=Bunch(replicas='4'),
                                  metadata=Bunch(name='pod'))])

    def patch_namespaced_job(self, *args, **kwargs):
        return Bunch(items=[Bunch(spec=Bunch(completions='0'),
                                  metadata=Bunch(name='pod'))])


class TestAutoscaler(object):  # pylint: disable=useless-object-inheritance

    def test_hget(self):
        redis_client = DummyRedis(fail_tolerance=2)
        kube_client = DummyKubernetes()
        scaler = autoscaler.Autoscaler(redis_client, kube_client, 'None',
                                       backoff_seconds=0.01)
        data = scaler.hget('rhash_new', 'status')
        assert data == 'new'
        assert scaler.redis_client.fail_count == redis_client.fail_tolerance

    def test_scan_iter(self):
        prefix = 'predict'
        redis_client = DummyRedis(fail_tolerance=2, prefix=prefix)
        kube_client = DummyKubernetes()
        scaler = autoscaler.Autoscaler(redis_client, kube_client, 'None',
                                       backoff_seconds=0.01)
        data = scaler.scan_iter(match=prefix)
        keys = [k for k in data]
        expected = [k for k in redis_client.keys() if k.startswith(prefix)]
        assert scaler.redis_client.fail_count == redis_client.fail_tolerance
        assert keys == expected

    def test_get_desired_pods(self):
        # key, keys_per_pod, min_pods, max_pods, current_pods
        redis_client = DummyRedis(fail_tolerance=2)
        kube_client = DummyKubernetes()
        scaler = autoscaler.Autoscaler(redis_client, kube_client, 'None',
                                       backoff_seconds=0.01)
        scaler.redis_keys['predict'] = 10
        # desired_pods is > max_pods
        desired_pods = scaler.get_desired_pods('predict', 2, 0, 2, 1)
        assert desired_pods == 2
        # desired_pods is < min_pods
        desired_pods = scaler.get_desired_pods('predict', 5, 9, 10, 0)
        assert desired_pods == 9
        # desired_pods is in range
        desired_pods = scaler.get_desired_pods('predict', 3, 0, 5, 1)
        assert desired_pods == 3
        # desired_pods is in range, current_pods exist
        desired_pods = scaler.get_desired_pods('predict', 10, 0, 5, 3)
        assert desired_pods == 3

    def test_get_current_pods(self):
        redis_client = DummyRedis(fail_tolerance=2)
        kube_client = DummyKubernetes()
        scaler = autoscaler.Autoscaler(redis_client, kube_client, 'None',
                                       backoff_seconds=0.01)

        # test invalid resource_type
        with pytest.raises(ValueError):
            scaler.get_current_pods('namespace', 'bad_type', 'pod')

        deployed_pods = scaler.get_current_pods('ns', 'deployment', 'pod1')
        assert deployed_pods == 4

        deployed_pods = scaler.get_current_pods('ns', 'deployment', 'pod2')
        assert deployed_pods == 8

        deployed_pods = scaler.get_current_pods('ns', 'job', 'pod1')
        assert deployed_pods == 1

        deployed_pods = scaler.get_current_pods('ns', 'job', 'pod2')
        assert deployed_pods == 2

    def test_tally_keys(self):
        redis_client = DummyRedis(fail_tolerance=2)
        kube_client = DummyKubernetes()
        scaler = autoscaler.Autoscaler(redis_client, kube_client, 'None',
                                       backoff_seconds=0.01)
        scaler.tally_keys()
        assert scaler.redis_keys == {'predict': 2, 'train': 2}

    def test_scale_deployments(self):
        redis_client = DummyRedis(fail_tolerance=2)
        kube_client = DummyKubernetes()
        deploy_params = ['0', '1', '3', 'ns', 'deployment', 'predict', 'name']
        job_params = ['1', '2', '1', 'ns', 'job', 'train', 'name']

        params = [deploy_params, job_params]

        param_delim = '|'
        deployment_delim = ';'

        # non-integer values will warn, but not raise (or autoscale)
        bad_params = ['f0', 'f1', 'f3', 'ns', 'job', 'train', 'name']
        p = deployment_delim.join([param_delim.join(bad_params)])

        scaler = autoscaler.Autoscaler(redis_client, kube_client, p, 0,
                                       deployment_delim, param_delim)
        scaler.scale_deployments()

        # not enough params will warn, but not raise (or autoscale)
        bad_params = ['0', '1', '3', 'ns', 'job', 'train']
        p = deployment_delim.join([param_delim.join(bad_params)])
        scaler = autoscaler.Autoscaler(redis_client, kube_client, p, 0,
                                       deployment_delim, param_delim)
        scaler.scale_deployments()

        # test bad resource_type
        with pytest.raises(ValueError):
            bad_params = ['0', '1', '3', 'ns', 'bad_type', 'train', 'name']
            p = deployment_delim.join([param_delim.join(bad_params)])
            scaler = autoscaler.Autoscaler(redis_client, kube_client, p, 0,
                                           deployment_delim, param_delim)
            scaler.scale_deployments()

        # test good delimiters and scaling params, bad resource_type
        deploy_params = ['0', '5', '1', 'ns', 'deployment', 'predict', 'name']
        job_params = ['1', '2', '1', 'ns', 'job', 'train', 'name']
        params = [deploy_params, job_params]
        p = deployment_delim.join([param_delim.join(p) for p in params])

        scaler = autoscaler.Autoscaler(redis_client, kube_client, p, 0,
                                       deployment_delim,
                                       param_delim)
        scaler.scale_deployments()
        # test desired_pods == current_pods
        scaler.get_desired_pods = lambda *x: 4
        scaler.scale_deployments()

        # same delimiter throws an error;
        with pytest.raises(ValueError):
            param_delim = '|'
            deployment_delim = '|'
            p = deployment_delim.join([param_delim.join(p) for p in params])
            autoscaler.Autoscaler(None, None, p, 0, deployment_delim, param_delim)
