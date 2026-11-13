"""Private transport boundaries and real read/draft service behavior (fake data only)."""
import datetime as dt
import json
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from billing import pdf, services as svc
from billing.models import AuditEvent, Customer, Document, NumberCounter, Product
from .conftest import central, line

pytestmark=pytest.mark.django_db


@pytest.fixture
def token_client(settings,blake):
    settings.CCB_PRIVATE_API_ENABLED=True
    settings.CCB_SERVICE_NETWORKS=['127.0.0.1/32']
    settings.CCB_MCP_ALLOWED_ORIGINS=[]
    row,raw=svc.create_token(blake)
    return Client(HTTP_AUTHORIZATION='Bearer '+raw),row


def call(client,operation,**arguments):
    return client.post('/api/v1/call/',{'operation':operation,'arguments':arguments},content_type='application/json')


def rpc(client,method,params=None,**headers):
    return client.post('/mcp',dict(jsonrpc='2.0',id=1,method=method,params=params or {}),content_type='application/json',HTTP_ACCEPT='application/json, text/event-stream',**headers)


def test_closed_by_default(settings,client):
    settings.CCB_PRIVATE_API_ENABLED=False
    assert call(client,'list_customers').status_code==404
    assert rpc(client,'initialize').status_code==404


def test_network_origin_token_and_cookie_boundaries(token_client,client,settings):
    authed,row=token_client
    assert call(authed,'list_customers').status_code==200
    assert authed.post('/api/v1/call/','{}',content_type='application/json',REMOTE_ADDR='203.0.113.10').status_code==403
    assert rpc(authed,'initialize',HTTP_ORIGIN='https://untrusted.example').status_code==403
    assert call(client,'list_customers').status_code==401
    owner=get_user_model().objects.create_user('sample-owner',is_superuser=True)
    client.force_login(owner)
    session=client.session;session['ccb_2fa_verified']=True;session.save()
    assert call(client,'list_customers').status_code==401
    row.revoked_at=__import__('django.utils.timezone',fromlist=['now']).now();row.save()
    assert call(authed,'list_customers').status_code==401


def test_create_read_preview_update_and_convert(token_client,customer,products,blake):
    client,_=token_client
    assert call(client,'get_customer',customer_id=customer.pk).json()['result']['open_balance_cents']==0
    payload=dict(kind='quote',customer_id=customer.pk,lines=[dict(product_id=products['build'].pk,unit_cents=75000),dict(product_id=products['hosting'].pk,unit_cents=6000)])
    response=call(client,'create_draft',**payload)
    assert response.status_code==200,response.content
    data=response.json()['result'];pk=data['id']
    assert data['number']=='' and data['prepared_by']=='ledger' and data['preview']['total_cents']==86250
    assert NumberCounter.objects.count()==0
    assert call(client,'update_draft',document_id=pk,notes='Sample review',email_body='Draft email only').status_code==200
    assert call(client,'preview_totals',document_id=pk).json()['result']['total_cents']==86250
    quote=Document.objects.get(pk=pk)
    quote=svc.approve(blake,quote,now=central(2026,12,10))
    svc.set_quote_outcome(blake,quote,'accepted')
    response=call(client,'convert_quote',document_id=pk)
    assert response.status_code==200
    assert response.json()['result']['status']=='draft' and response.json()['result']['from_quote_id']==pk
    refused=call(client,'update_draft',document_id=pk,notes='Cannot edit approval')
    assert refused.status_code==400
    assert AuditEvent.objects.filter(action='denied:edit').exists()


@pytest.mark.parametrize('op',['approve','send','mark_sent','void','record_payment','resend_receipt','update_settings','confirm_tax_location','delete_draft'])
def test_forbidden_operations_are_logged(token_client,op):
    client,_=token_client
    before=AuditEvent.objects.count()
    assert call(client,op).status_code==403
    assert AuditEvent.objects.count()==before+1


@pytest.mark.parametrize('field,value',[('actor','blake'),('number','CC-2026-9999'),('status','paid'),('tax_exempt',True),('donated',True),('tax_override_state','IA')])
def test_protected_fields_never_mass_assigned(token_client,field,value):
    client,_=token_client
    assert call(client,'create_customer',name='Sample',**{field:value}).status_code==403
    assert Customer.objects.count()==0


def test_schema_strictness_and_no_token_leaks(token_client,customer,products):
    client,row=token_client
    for cents in [True,1.0,-1,'100',None]:
        response=call(client,'create_draft',kind='invoice',customer_id=customer.pk,lines=[dict(product_id=products['build'].pk,unit_cents=cents)])
        assert response.status_code==400,(cents,response.content)
    assert Document.objects.count()==0
    response=call(client,'list_customers',limit=1000)
    assert response.status_code==400
    response=client.post('/api/v1/call/','{"operation":"list_customers","operation":"approve"}',content_type='application/json')
    assert response.status_code==400
    response=call(client,'list_customers')
    assert row.token_hash.encode() not in response.content and b'token_hash' not in response.content


def test_contact_change_proposal_and_read_scope(token_client,blake,customer):
    client,_=token_client
    response=call(client,'update_customer',customer_id=customer.pk,address='200 Sample Ave, Hull, IA')
    assert response.status_code==200 and response.json()['result']['tax_location_status']=='pending Blake'
    loc=call(client,'list_tax_locations',q='Hull').json()['result']['items'][0]
    response=call(client,'propose_tax_location',customer_id=customer.pk,state='IA',jurisdiction_id=loc['id'],checked_on='2026-11-13',source='Sample DOR check')
    assert response.status_code==200 and response.json()['result']['status']=='pending'
    customer.refresh_from_db();assert customer.tax_jurisdiction.name=='Sioux Center'
    _,raw=svc.create_token(blake,scopes=('read',))
    read=Client(HTTP_AUTHORIZATION='Bearer '+raw)
    assert call(read,'list_customers').status_code==200
    assert call(read,'create_customer',name='No').status_code==403
    tools=rpc(read,'tools/list').json()['result']['tools']
    assert 'create_draft' not in [t['name'] for t in tools]


def test_mcp_initialize_list_call_and_version_boundary(token_client):
    client,_=token_client
    response=rpc(client,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'sample','version':'1'}})
    assert response.status_code==200 and response.json()['result']['protocolVersion']=='2025-11-25'
    names=[t['name'] for t in rpc(client,'tools/list').json()['result']['tools']]
    assert 'create_draft' in names and 'send' not in names and 'approve' not in names
    assert rpc(client,'tools/call',{'name':'list_customers','arguments':{}}).json()['result']['structuredContent']['total']==0
    assert rpc(client,'tools/call',{'name':'approve','arguments':{}}).json()['result']['isError'] is True
    assert rpc(client,'tools/list',HTTP_MCP_PROTOCOL_VERSION='1900-01-01').status_code==400
    assert client.get('/mcp',HTTP_ACCEPT='application/json, text/event-stream').status_code==405
    assert client.post('/mcp',{'jsonrpc':'2.0','method':'notifications/initialized'},content_type='application/json',HTTP_ACCEPT='application/json, text/event-stream').status_code==202


def test_mcp_pdf_and_history(token_client,blake,customer,products,settings,tmp_path):
    settings.MEDIA_ROOT=tmp_path
    client,_=token_client
    doc=svc.create_draft(blake,'invoice',customer,[line(products['build'],10000)])
    doc=svc.approve(blake,doc,now=central(2026,12,10),render_pdf=pdf.render_approved)
    response=rpc(client,'tools/call',{'name':'download_pdf','arguments':{'document_id':doc.pk}})
    assert response.status_code==200
    resource=response.json()['result']['content'][0]['resource']
    import base64
    assert resource['mimeType']=='application/pdf' and base64.b64decode(resource['blob']).startswith(b'%PDF')
    assert call(client,'get_history',document_id=doc.pk).json()['result']['total']>=2
    assert call(client,'list_payments',document_id=doc.pk).json()['result']['items']==[]
    assert call(client,'list_receipts',document_id=doc.pk).json()['result']['items']==[]


def test_token_command_writes_private_file_once_without_logging_key(blake,tmp_path):
    import io,os
    from django.core.management import call_command,CommandError
    path=tmp_path/'sample-token'
    stdout=io.StringIO()
    call_command('create_ledger_token',output=str(path),read_only=True,stdout=stdout)
    raw=path.read_text().strip()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert raw not in stdout.getvalue()
    assert svc.authenticate_token(raw).scopes==frozenset({'read'})
    with pytest.raises(CommandError): call_command('create_ledger_token',output=str(path))
    assert path.read_text().strip()==raw
