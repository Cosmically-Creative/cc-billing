"""Write a newly generated Ledger token once to a private local file; never print it."""
import os
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from billing import services
from billing.permissions import ActorRef


class Command(BaseCommand):
    help='Create a read/draft Ledger token in a new mode-600 file. Never logs the token.'

    def add_arguments(self,parser):
        parser.add_argument('--output',required=True)
        parser.add_argument('--read-only',action='store_true')

    def handle(self,output,read_only=False,**options):
        path=Path(output).expanduser().absolute()
        fd=None
        created=False
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            created=True
            with transaction.atomic():
                row,raw=services.create_token(ActorRef.blake('local-operator'),scopes=('read',) if read_only else ('read','drafts:write'))
                with os.fdopen(fd,'w') as file:
                    fd=None
                    file.write(raw+'\n');file.flush();os.fsync(file.fileno())
        except FileExistsError:
            raise CommandError('Output already exists; nothing was replaced.')
        except Exception:
            if fd is not None: os.close(fd)
            # Only this command's newly created file may be removed after an aborted token insert.
            if created and path.exists(): path.unlink()
            raise
        self.stdout.write(f'Ledger token {row.prefix} written to the private output file.')
