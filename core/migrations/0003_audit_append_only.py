from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION hub_audit_block() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit events are append-only';
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS core_auditevent_append_only ON core_auditevent;
CREATE TRIGGER core_auditevent_append_only BEFORE UPDATE OR DELETE ON core_auditevent
FOR EACH ROW EXECUTE FUNCTION hub_audit_block();
"""
REVERSE = "DROP TRIGGER IF EXISTS core_auditevent_append_only ON core_auditevent; DROP FUNCTION IF EXISTS hub_audit_block();"


class Migration(migrations.Migration):
    dependencies = [("core", "0002_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
