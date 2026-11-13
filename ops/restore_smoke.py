"""CI-only sample DB/media dump and restore. Never touches the configured app database.

Requires a disposable local PostgreSQL service container. Generates its own database names;
never accepts an existing database name as a target. No SMTP, Stripe or offsite upload.
"""
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
container=os.environ.get('PG_SERVICE_CONTAINER_ID','')
host=os.environ.get('POSTGRES_HOST','')
if host not in ['localhost','127.0.0.1'] or not re.fullmatch('[a-f0-9]{12,64}',container):
    raise SystemExit('Restore smoke requires the disposable local CI PostgreSQL service container.')
user=os.environ['POSTGRES_USER']
admin=psycopg.connect(host=host,port=os.environ.get('POSTGRES_PORT','5432'),user=user,password=os.environ.get('POSTGRES_PASSWORD',''),dbname='postgres',autocommit=True)
names=['ccb_restore_smoke_'+uuid.uuid4().hex[:12] for _ in range(2)]
created=[]
try:
    for name in names:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        created.append(name)
    with tempfile.TemporaryDirectory(prefix='cc-billing-restore-') as temporary:
        root=Path(temporary)
        os.environ.update(DJANGO_SETTINGS_MODULE='config.settings',POSTGRES_DB=names[0],CCB_MEDIA_ROOT=str(root/'source-media'),CCB_STRIPE_ENABLED='0',CCB_STRIPE_ACCEPT_WEBHOOKS='0',CCB_EMAIL_ENABLED='0')
        import django
        django.setup()
        from django.apps import apps
        from django.conf import settings
        from django.core.management import call_command
        from django.db import connections,transaction,DatabaseError
        from billing import audit,pdf,services as svc
        from billing.models import Document,Product,TaxJurisdiction
        from billing.permissions import ActorRef
        call_command('migrate',verbosity=0,interactive=False)
        actor=ActorRef.blake('sample-restore')
        customer=svc.create_customer(actor,name='Sample restore customer',email='sample@example.com')
        customer=svc.confirm_tax_location(actor,customer,'IA',TaxJurisdiction.objects.get(name='Sioux Center'),source='Sample restore fixture')
        product=Product.objects.get(name='Website build')
        when=dt.datetime(2026,12,10,12,tzinfo=ZoneInfo('America/Chicago'))
        doc=svc.create_draft(actor,'invoice',customer,[dict(product=product,unit_cents=10000)])
        doc=svc.approve(actor,doc,now=when,render_pdf=pdf.render_approved)
        svc.mark_sent(actor,doc,now=when)
        payment=svc.record_manual_payment(actor,doc,'check',10700,dt.date(2026,12,12),check_number='SAMPLE')
        svc.deliver_receipt(payment.receipt,pdf.render_paid_copy,lambda *args:None)
        models=list(apps.get_app_config('billing').get_models())
        counts={model._meta.label:model.objects.count() for model in models}
        head=audit.AuditEvent.objects.order_by('-pk').first().hash
        assert audit.verify_chain()[0]
        media_backup=root/'backup-media'
        shutil.copytree(settings.MEDIA_ROOT,media_backup)
        with (root/'sample.dump').open('wb') as stream:
            subprocess.run(['docker','exec',container,'pg_dump','-U',user,'-d',names[0],'-Fc'],stdout=stream,stderr=subprocess.PIPE,check=True)
        with (root/'sample.dump').open('rb') as stream:
            subprocess.run(['docker','exec','-i',container,'pg_restore','-U',user,'-d',names[1],'--no-owner','--no-acl'],stdin=stream,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)
        connections.close_all()
        settings.DATABASES['default']['NAME']=names[1]
        connections['default'].settings_dict['NAME']=names[1]
        settings.MEDIA_ROOT=root/'restored-media'
        shutil.copytree(media_backup,settings.MEDIA_ROOT)
        assert counts=={model._meta.label:model.objects.count() for model in models},'Restored row counts differ'
        assert audit.verify_chain()[0] and audit.AuditEvent.objects.order_by('-pk').first().hash==head,'Restored audit chain differs'
        from billing.models import PaidCopy
        for model in [Document,PaidCopy]:
            for item in model.objects.exclude(pdf_file=''):
                path=(settings.MEDIA_ROOT/item.pdf_file).resolve()
                assert path.is_relative_to(settings.MEDIA_ROOT.resolve())
                assert hashlib.sha256(path.read_bytes()).hexdigest()==item.pdf_sha256,'Restored PDF hash differs'
        try:
            with transaction.atomic():
                audit.AuditEvent.objects.filter(pk=audit.AuditEvent.objects.first().pk).update(note='tampering test')
        except DatabaseError: pass
        else: raise AssertionError('Restored database lost its append-only guard')
        customer=type(customer).objects.get(pk=customer.pk)
        next_doc=svc.create_draft(actor,'invoice',customer,[dict(product=Product.objects.get(pk=product.pk),unit_cents=10000)])
        next_doc=svc.approve(actor,next_doc,now=when,render_pdf=pdf.render_approved)
        assert next_doc.number=='CC-2026-0002','Restored counter did not continue'
        print('Sample restore smoke passed: row counts, audit chain, stored PDFs, DB guard and next invoice number.')
        connections.close_all()
finally:
    if 'django' in sys.modules:
        from django.db import connections
        connections.close_all()
    for name in created:
        admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
    admin.close()
