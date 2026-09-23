# Written to match what `makemigrations` generates for the Membership
# manager change (Phase 2, Step 50). State-only: no SQL is executed.

import django.db.models.manager
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0002_room_room_direct_key_matches_type'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='membership',
            options={'base_manager_name': 'all_objects', 'default_manager_name': 'objects'},
        ),
        migrations.AlterModelManagers(
            name='membership',
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
    ]
