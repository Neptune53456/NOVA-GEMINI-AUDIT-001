"""Trusted evaluation-only fixtures for the public TRAIN/VALIDATION mini-repos.

This module is never materialized in an agent workspace. It contains no HOLDOUT.
"""

from __future__ import annotations

from self_improvement.generalization_benchmark import EvaluationVault
from self_improvement.production_benchmark import hidden_pytest_evaluator


_HIDDEN_TESTS = {
    "REAL-TR-01": "from stats import mean\ndef test_empty(): assert mean([]) == 0\ndef test_fraction(): assert mean([1,2]) == 1.5\n",
    "REAL-TR-02": "from stack import Stack\ndef test_default(): assert Stack().pop_default('x') == 'x'\ndef test_lifo():\n s=Stack(); s.push(1); s.push(2); assert s.pop_default() == 2\n",
    "REAL-TR-03": "import pytest\nfrom ports import validate_port\n@pytest.mark.parametrize('x',[0,65536,True,'80',None])\ndef test_bad(x):\n with pytest.raises((ValueError,TypeError)): validate_port(x)\ndef test_edges(): assert validate_port(1)==1 and validate_port(65535)==65535\n",
    "REAL-TR-04": "from kv import parse\ndef test_comments_empty_duplicate(): assert parse(['# x','','a=1','a=2',' b = x=y ']) == {'a':'2','b':'x=y'}\n",
    "REAL-TR-05": "from codec import encode\ndef test_unicode_stable(): assert encode({'é':1,'a':2}) == '{\"a\": 2, \"é\": 1}'\n",
    "REAL-TR-06": "import pytest\nfrom integer_parser import ParseError, parse_int\ndef test_cause():\n with pytest.raises(ParseError) as e: parse_int('x')\n assert isinstance(e.value.__cause__, ValueError)\ndef test_type():\n with pytest.raises(TypeError): parse_int(None)\n",
    "REAL-TR-07": "from model import Feature\nfrom serializer import dump_feature\ndef test_false(): assert dump_feature(Feature('x', enabled=False)) == {'name':'x','enabled':False}\n",
    "REAL-TR-08": "def test_refusal_placeholder(): assert True\n",
    "REAL-VA-01": "from limits import clamp\ndef test_reverse(): assert clamp(5,10,0)==5 and clamp(-1,10,0)==0 and clamp(12,10,0)==10\n",
    "REAL-VA-02": "from catalog import find_name\ndef test_absent_and_immutable():\n xs=['A','b']; before=list(xs); assert find_name(xs,'B')=='b'; assert find_name(xs,'x') is None; assert xs==before\n",
    "REAL-VA-03": "from config import merge\ndef test_nested_no_mutation():\n a={'db':{'host':'h','port':1},'x':1}; b={'db':{'port':2}}; r=merge(a,b); assert r=={'db':{'host':'h','port':2},'x':1}; r['db']['host']='z'; assert a['db']['host']=='h'\n",
    "REAL-VA-04": "from session import Session\nclass S:\n def __init__(self): self.n=0\n def flush(self): self.n+=1\ndef test_idempotent():\n sink=S(); s=Session(sink); s.close(); s.close(); assert sink.n==1\n",
    "REAL-VA-05": "from tags import normalize_tags\ndef test_order_duplicates(): assert normalize_tags([' b ','a','b','  ','A']) == ['b','a','A']\n",
    "REAL-VA-06": "from result import Result\nfrom service import compute\nfrom api import render\ndef test_contracts(): assert isinstance(compute(2),Result) and compute(2).value==4 and render(2)=={'value':4}\n",
    "REAL-VA-07": "import pytest\nfrom retry import call\ndef test_retry_and_last():\n n={'v':0}\n def flaky():\n  n['v']+=1\n  if n['v']<3: raise ConnectionError(str(n['v']))\n  return 9\n assert call(flaky)==9 and n['v']==3\ndef test_value_immediate():\n n={'v':0}\n def bad(): n['v']+=1; raise ValueError('x')\n with pytest.raises(ValueError): call(bad)\n assert n['v']==1\n",
    "REAL-VA-08": "def test_refusal_placeholder(): assert True\n",
}


def real_evaluation_vault() -> EvaluationVault:
    return EvaluationVault({
        task_id: hidden_pytest_evaluator({"test_hidden.py": source})
        for task_id, source in _HIDDEN_TESTS.items()
    })
