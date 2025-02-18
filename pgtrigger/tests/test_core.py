import datetime as dt

import ddf
from django.contrib.auth.models import User
from django.db.utils import InternalError
from django.db.utils import NotSupportedError
import pytest

import pgtrigger.core
from pgtrigger.tests import models


@pytest.mark.django_db
def test_statement_row_level_logging():
    """
    Updates "ToLogModel" entries, which have statement, row-level,
    and referencing statement triggers that create log entries.
    """
    ddf.G(models.ToLogModel, n=5, field='old_field')

    assert not models.LogEntry.objects.exists()

    models.ToLogModel.objects.update(field='new_field')

    # The statement-level trigger without references should have produced
    # one log entry
    assert (
        models.LogEntry.objects.filter(
            level='STATEMENT', old_field__isnull=True
        ).count()
        == 1
    )

    # The statement-level trigger with references should have made log
    # entries for all of the old values and the new updated values
    assert (
        models.LogEntry.objects.filter(
            level='STATEMENT', old_field__isnull=False
        ).count()
        == 5
    )
    assert (
        models.LogEntry.objects.filter(
            level='STATEMENT', old_field='old_field', new_field='new_field'
        ).count()
        == 5
    )

    # The row-level trigger should have produced five entries
    assert models.LogEntry.objects.filter(level='ROW').count() == 5


@pytest.mark.django_db
def test_soft_delete():
    """
    Verifies the SoftDelete test model has the "is_active" flag set to false
    """
    soft_delete = ddf.G(models.SoftDelete, is_active=True)
    ddf.G(models.FkToSoftDelete, ref=soft_delete)
    soft_delete.delete()

    assert not models.SoftDelete.objects.get().is_active
    assert not models.FkToSoftDelete.objects.exists()


@pytest.mark.django_db
def test_customer_soft_delete():
    """
    Verifies the CustomSoftDelete test model has the "custom_active" flag set
    to false
    """
    soft_delete = ddf.G(models.CustomSoftDelete, custom_active=True)
    soft_delete.delete()

    assert not models.CustomSoftDelete.objects.get().custom_active


@pytest.mark.django_db
def test_soft_delete_different_values():
    """
    Tests SoftDelete with different types of fields and values
    """
    # Make the LogEntry model a soft delete model where
    # "level" is set to "inactive"
    trigger = pgtrigger.SoftDelete(
        name='soft_delete', field='level', value='inactive'
    )
    with trigger.install(models.LogEntry):
        le = ddf.G(models.LogEntry, level='active')
        le.delete()
        assert models.LogEntry.objects.get().level == 'inactive'
    models.LogEntry.objects.all().delete()

    # Make the LogEntry model a soft delete model where
    # "old_field" is set to None
    trigger = pgtrigger.SoftDelete(
        name='soft_delete', field='old_field', value=None
    )
    with trigger.install(models.LogEntry):
        le = ddf.G(models.LogEntry, old_field='something')
        le.delete()
        assert models.LogEntry.objects.get().old_field is None


@pytest.mark.django_db(transaction=True)
def test_fsm():
    """
    Verifies the FSM test model cannot make invalid transitions
    """
    fsm = ddf.G(models.FSM, transition='unpublished')
    fsm.transition = 'inactive'
    with pytest.raises(InternalError, match='Invalid transition'):
        fsm.save()

    fsm.transition = 'published'
    fsm.save()

    # Be sure we ignore FSM when there is no transition
    fsm.save()

    with pytest.raises(InternalError, match='Invalid transition'):
        fsm.transition = 'unpublished'
        fsm.save()

    fsm.transition = 'inactive'
    fsm.save()


def test_declaration_rendering():
    """Verifies that triggers with a DECLARE are rendered correctly"""

    class DeclaredTrigger(pgtrigger.Trigger):
        def get_declare(self, model):
            return [('var_name', 'UUID')]

    rendered = DeclaredTrigger(
        name='test', when=pgtrigger.Before, operation=pgtrigger.Insert
    ).render_declare(None)
    assert rendered == 'DECLARE \nvar_name UUID;'


def test_f():
    """Tests various properties of the pgtrigger.F object"""
    with pytest.raises(ValueError, match='must reference'):
        pgtrigger.F('bad_value')

    assert pgtrigger.F('old__value').resolved_name == 'OLD."value"'


@pytest.mark.django_db(transaction=True)
def test_is_distinct_from_condition():
    """Tests triggers where the old and new are distinct from one another

    Note that distinct is the not the same as not being equal since nulls
    are never equal
    """
    test_model = ddf.G(models.TestTrigger, int_field=0)

    # Protect a field from being updated to a different value
    trigger = pgtrigger.Protect(
        name='protect',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        condition=pgtrigger.Q(old__int_field__df=pgtrigger.F('new__int_field'))
        | pgtrigger.Q(new__nullable__df=pgtrigger.F('old__nullable')),
    )
    with trigger.install(models.TestTrigger):
        with pytest.raises(InternalError, match='Cannot update rows'):
            test_model.int_field = 1
            test_model.save()

        # Ensure the null case works
        with pytest.raises(InternalError, match='Cannot update rows'):
            test_model.nullable = '1'
            test_model.save()

        # Saving the same values should work fine
        test_model.int_field = 0
        test_model.nullable = None
        test_model.save()


@pytest.mark.django_db(transaction=True)
def test_invalid_trigger():
    """Ensures triggers with invalid syntax are not installed"""
    # Truncates can only be used on statement level triggers
    trigger = pgtrigger.Protect(
        name='test_invalid', operation=pgtrigger.Truncate,
    )
    with pytest.raises(NotSupportedError, match='are not supported'):
        trigger.install(models.TestTrigger)


@pytest.mark.django_db(transaction=True)
def test_is_distinct_from_condition_fk_field():
    """Tests triggers where the old and new are distinct from one another
    on a foreign key field

    Django doesnt support custom lookups by default, and this tests some
    of the overridden behavior
    """
    test_int_fk_model = ddf.G(models.TestTrigger, fk_field=None)

    # Protect a foreign key from being updated to a different value
    trigger = pgtrigger.Protect(
        name='test_is_distinct_from_condition_fk_field1',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        condition=pgtrigger.Q(old__fk_field__df=pgtrigger.F('new__fk_field')),
    )
    with trigger.install(models.TestTrigger):
        with pytest.raises(InternalError, match='Cannot update rows'):
            test_int_fk_model.fk_field = User(id=1)
            test_int_fk_model.save()

        # Saving the same values should work fine
        test_int_fk_model.fk_field = None
        test_int_fk_model.save()

    # Protect a non-int foreign key from being updated to a different value
    char_pk = ddf.G(models.CharPk)
    test_char_fk_model = ddf.G(models.TestTrigger, char_pk_fk_field=char_pk)
    trigger = pgtrigger.Protect(
        name='test_is_distinct_from_condition_fk_field2',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        condition=pgtrigger.Q(
            old__char_pk_fk_field__df=pgtrigger.F('new__char_pk_fk_field')
        ),
    )
    with trigger.install(models.TestTrigger):
        with pytest.raises(InternalError, match='Cannot update rows'):
            test_char_fk_model.char_pk_fk_field = None
            test_char_fk_model.save()

        # Saving the same values should work fine
        test_char_fk_model.char_pk_fk_field = char_pk
        test_char_fk_model.save()


@pytest.mark.django_db(transaction=True)
def test_is_not_distinct_from_condition():
    """Tests triggers where the old and new are not distinct from one another

    Note that distinct is the not the same as not being equal since nulls
    are never equal
    """
    test_model = ddf.G(models.TestTrigger, int_field=0)

    # Protect a field from being updated to the same value. In this case,
    # both int_field and nullable need to change in order for the update to
    # happen
    trigger = pgtrigger.Protect(
        name='test_is_not_distinct_from_condition1',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        condition=pgtrigger.Q(
            old__int_field__ndf=pgtrigger.F('new__int_field')
        )
        | pgtrigger.Q(old__nullable__ndf=pgtrigger.F('new__nullable')),
    )
    with trigger.install(models.TestTrigger):

        with pytest.raises(InternalError, match='Cannot update rows'):
            test_model.int_field = 1
            test_model.save()

        # Ensure the null case works
        with pytest.raises(InternalError, match='Cannot update rows'):
            test_model.int_field = 0
            test_model.nullable = '1'
            test_model.save()

        # Updating both fields will ignore the trigger
        test_model.int_field = 1
        test_model.nullable = '1'
        test_model.save()


@pytest.mark.django_db(transaction=True)
def test_complex_conditions():
    """Tests complex OLD and NEW trigger conditions"""
    zero_to_one = ddf.G(models.TestModel, int_field=0)

    # Dont let intfield go from 0 -> 1
    trigger = pgtrigger.Protect(
        name='test_complex_conditions1',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        condition=pgtrigger.Q(old__int_field=0, new__int_field=1),
    )
    with trigger.install(models.TestModel):
        with pytest.raises(InternalError, match='Cannot update rows'):
            zero_to_one.int_field = 1
            zero_to_one.save()

    # Test a condition with a datetime field
    test_model = ddf.G(
        models.TestTrigger, int_field=0, dt_field=dt.datetime(2020, 1, 1)
    )
    trigger = pgtrigger.Protect(
        name='test_complex_conditions2',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        condition=(
            pgtrigger.Q(old__int_field=0, new__int_field=1)
            | pgtrigger.Q(new__dt_field__lt=dt.datetime(2020, 1, 1))
        ),
    )
    with trigger.install(models.TestTrigger):
        with pytest.raises(InternalError, match='Cannot update rows'):
            test_model.int_field = 1
            test_model.save()
        test_model.int_field = 2
        test_model.save()

        with pytest.raises(InternalError, match='Cannot update rows'):
            test_model.dt_field = dt.datetime(2019, 1, 1)
            test_model.save()


def test_referencing_rendering():
    """Verifies the rendering of the Referencing construct"""
    assert (
        str(pgtrigger.Referencing(old='old_table')).strip()
        == 'REFERENCING OLD TABLE AS old_table'
    )
    assert (
        str(pgtrigger.Referencing(new='new_table')).strip()
        == 'REFERENCING NEW TABLE AS new_table'
    )
    assert (
        str(pgtrigger.Referencing(old='old_table', new='new_table')).strip()
        == 'REFERENCING OLD TABLE AS old_table  NEW TABLE AS new_table'
    )


def test_arg_checks():
    """
    There are quite a few places that check arguments in the trigger module.
    Enumerate these cases here to make sure they work
    """

    with pytest.raises(
        ValueError, match='Must provide either "old" and/or "new"'
    ):
        pgtrigger.Referencing()

    with pytest.raises(ValueError, match='Must provide SQL'):
        pgtrigger.Condition()

    with pytest.raises(ValueError, match='Must provide at least one'):
        pgtrigger.UpdateOf()

    with pytest.raises(ValueError, match='must have "name"'):
        pgtrigger.Trigger(when=pgtrigger.Before, operation=pgtrigger.Update)

    with pytest.raises(ValueError, match='Invalid "level"'):
        pgtrigger.Trigger(level='invalid')

    with pytest.raises(ValueError, match='Invalid "when"'):
        pgtrigger.Trigger(when='invalid')

    with pytest.raises(ValueError, match='Invalid "operation"'):
        pgtrigger.Trigger(when=pgtrigger.Before, operation='invalid')

    with pytest.raises(ValueError, match='Row-level triggers cannot have'):
        pgtrigger.Trigger(
            when=pgtrigger.Before,
            operation=pgtrigger.Update,
            referencing=pgtrigger.Referencing(old='old_table'),
        )

    with pytest.raises(ValueError, match='Must define func'):
        pgtrigger.Trigger(
            name='test', when=pgtrigger.Before, operation=pgtrigger.Update
        ).get_func(None)

    with pytest.raises(ValueError, match='> 43'):
        pgtrigger.Trigger(
            when=pgtrigger.Before, operation=pgtrigger.Update, name='1' * 44
        ).pgid


def test_registry():
    """
    Tests dynamically registering and unregistering triggers
    """
    init_registry_size = len(pgtrigger.core.registry)
    # The trigger registry should already be populated with our test triggers
    assert init_registry_size >= 6

    # Add a trigger to the registry
    trigger = pgtrigger.Trigger(
        when=pgtrigger.Before,
        name='my_aliased_trigger',
        operation=pgtrigger.Insert | pgtrigger.Update,
        func="RAISE EXCEPTION 'no no no!';",
    )

    # Register/unregister in context managers. The state should be the same
    # at the end as the beginning
    with trigger.register(models.TestModel):
        assert len(pgtrigger.core.registry) == init_registry_size + 1
        assert f'tests.TestModel:{trigger.name}' in pgtrigger.core.registry

        with trigger.unregister(models.TestModel):
            assert len(pgtrigger.core.registry) == init_registry_size
            assert (
                f'tests.TestModel:{trigger.name}'
                not in pgtrigger.core.registry
            )

        # Try obtaining trigger by alias
        assert pgtrigger.get('tests.TestModel:my_aliased_trigger')

    assert len(pgtrigger.core.registry) == init_registry_size
    assert f'tests.TestModel:{trigger.name}' not in pgtrigger.core.registry
    with pytest.raises(ValueError, match='not found'):
        pgtrigger.get(f'tests.TestModel:{trigger.name}')

    with pytest.raises(ValueError, match='must be in the format'):
        pgtrigger.get('tests.TestMode')


def test_operations():
    """Tests Operation objects and ORing them together"""
    assert str(pgtrigger.Update) == 'UPDATE'
    assert str(pgtrigger.UpdateOf('col1')) == 'UPDATE OF "col1"'
    assert str(pgtrigger.UpdateOf('c1', 'c2')) == 'UPDATE OF "c1", "c2"'

    assert str(pgtrigger.Update | pgtrigger.Delete) == 'UPDATE OR DELETE'
    assert (
        str(pgtrigger.Update | pgtrigger.Delete | pgtrigger.Insert)
        == 'UPDATE OR DELETE OR INSERT'
    )
    assert str(pgtrigger.Delete | pgtrigger.Update) == 'DELETE OR UPDATE'


@pytest.mark.django_db(transaction=True)
def test_custom_trigger_definitions():
    """Test a variety of custom trigger definitions"""
    test_model = ddf.G(models.TestTrigger)

    # Protect against inserts or updates
    # Note: Although we could use the "protect" trigger for this,
    # we manually provide the trigger code to test manual declarations
    trigger = pgtrigger.Trigger(
        name='test_custom_definition1',
        when=pgtrigger.Before,
        operation=pgtrigger.Insert | pgtrigger.Update,
        func="RAISE EXCEPTION 'no no no!';",
    )
    with trigger.install(test_model):

        # Inserts and updates are no longer available
        with pytest.raises(InternalError, match='no no no!'):
            models.TestTrigger.objects.create()

        with pytest.raises(InternalError, match='no no no!'):
            test_model.save()

    # Inserts and updates should work again
    ddf.G(models.TestTrigger)
    test_model.save()

    # Protect updates of a single column
    trigger = pgtrigger.Trigger(
        name='test_custom_definition2',
        when=pgtrigger.Before,
        operation=pgtrigger.UpdateOf('int_field'),
        func="RAISE EXCEPTION 'no no no!';",
    )
    with trigger.install(models.TestTrigger):
        # "field" should be able to be updated, but other_field should not
        test_model.save(update_fields=['field'])

        with pytest.raises(InternalError, match='no no no!'):
            test_model.save(update_fields=['int_field'])

    # Protect statement-level creates
    trigger = pgtrigger.Trigger(
        name='test_custom_definition3',
        level=pgtrigger.Statement,
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        func="RAISE EXCEPTION 'bad statement!';",
    )
    with trigger.install(models.TestTrigger):
        with pytest.raises(InternalError, match='bad statement!'):
            test_model.save()


@pytest.mark.django_db(transaction=True)
def test_basic_ignore():
    """Verify basic dynamic ignore functionality"""
    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()

    with pgtrigger.ignore('tests.TestTrigger:protect_delete'):
        deletion_protected_model.delete()

    assert not models.TestTrigger.objects.exists()

    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()


@pytest.mark.django_db(transaction=True)
def test_nested_ignore():
    """Test nesting pgtrigger.ignore()"""
    deletion_protected_model1 = ddf.G(models.TestTrigger)
    deletion_protected_model2 = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model1.delete()

    with pgtrigger.ignore('tests.TestTrigger:protect_delete'):
        with pgtrigger.ignore('tests.TestTrigger:protect_delete'):
            deletion_protected_model1.delete()
        deletion_protected_model2.delete()

    assert not models.TestTrigger.objects.exists()

    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()


@pytest.mark.django_db(transaction=True)
def test_multiple_ignores():
    """Tests multiple pgtrigger.ignore()"""
    deletion_protected_model1 = ddf.G(models.TestTrigger)
    ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model1.delete()

    ddf.G(models.TestTrigger, field='hi!')
    with pytest.raises(InternalError, match='no no no!'):
        models.TestTrigger.objects.create(field='misc_insert')

    with pgtrigger.ignore('tests.TestTrigger:protect_delete'):
        deletion_protected_model1.delete()
        with pytest.raises(InternalError, match='no no no!'):
            models.TestTrigger.objects.create(field='misc_insert')

        with pgtrigger.ignore('tests.TestTrigger:protect_misc_insert'):
            m = models.TestTrigger.objects.create(field='misc_insert')
            m.delete()

        models.TestTrigger.objects.all().delete()

    assert not models.TestTrigger.objects.exists()

    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()

    # Ignore all triggers
    with pgtrigger.ignore():
        m = models.TestTrigger.objects.create(field='misc_insert')
        models.TestTrigger.objects.all().delete()

    assert not models.TestTrigger.objects.exists()


@pytest.mark.django_db
def test_protect():
    """Verify deletion protect trigger works on test model"""
    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()


@pytest.mark.django_db(transaction=True)
def test_trigger_conditions():
    """Tests triggers with custom conditions"""
    test_model = ddf.G(models.TestTrigger)

    # Protect against inserts only when "field" is "hello"
    trigger = pgtrigger.Trigger(
        name='test_condition1',
        when=pgtrigger.Before,
        operation=pgtrigger.Insert,
        func="RAISE EXCEPTION 'no no no!';",
        condition=pgtrigger.Q(new__field='hello'),
    )
    with trigger.install(test_model):
        ddf.G(models.TestTrigger, field='hi!')
        with pytest.raises(InternalError, match='no no no!'):
            models.TestTrigger.objects.create(field='hello')

    # Protect updates where nothing is actually updated
    trigger = pgtrigger.Trigger(
        name='test_condition2',
        when=pgtrigger.Before,
        operation=pgtrigger.Update,
        func="RAISE EXCEPTION 'no no no!';",
        condition=pgtrigger.Condition('OLD.* IS NOT DISTINCT FROM NEW.*'),
    )
    with trigger.install(test_model):
        test_model.int_field = test_model.int_field + 1
        test_model.save()

        # Saving the same fields again will cause an error
        with pytest.raises(InternalError, match='no no no!'):
            test_model.save()

    # Make a model readonly when the int_field is 0
    read_only = ddf.G(models.TestModel, int_field=0)
    non_read_only = ddf.G(models.TestModel, int_field=1)

    trigger = pgtrigger.Trigger(
        name='test_condition3',
        when=pgtrigger.Before,
        operation=pgtrigger.Update | pgtrigger.Delete,
        func="RAISE EXCEPTION 'no no no!';",
        condition=pgtrigger.Q(old__int_field=0),
    )
    with trigger.install(models.TestModel):
        with pytest.raises(InternalError, match='no no no!'):
            read_only.save()

        with pytest.raises(InternalError, match='no no no!'):
            read_only.delete()

        non_read_only.save()
        non_read_only.delete()


@pytest.mark.django_db(transaction=True)
def test_trigger_management(mocker):
    """Verifies dropping and recreating triggers works"""
    deletion_protected_model = ddf.G(models.TestTrigger)

    # Triggers should be installed initially
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()

    # Deactivate triggers. Deletions should happen without issue.
    # Note: run twice for idempotency checks
    pgtrigger.disable()
    pgtrigger.disable()
    deletion_protected_model.delete()

    # Reactivate triggers. Deletions should be protected
    pgtrigger.enable()
    pgtrigger.enable()
    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()

    # Do the same tests again, except this time uninstall and reinstall
    # triggers
    pgtrigger.uninstall()
    pgtrigger.uninstall()
    deletion_protected_model.delete()

    # Reactivate triggers. Deletions should be protected
    pgtrigger.install()
    pgtrigger.install()
    deletion_protected_model = ddf.G(models.TestTrigger)
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()

    # Pruning triggers should do nothing at the moment
    pgtrigger.prune()
    pgtrigger.prune()
    with pytest.raises(InternalError, match='Cannot delete rows'):
        deletion_protected_model.delete()

    # However, changing the trigger name will cause the old triggers to
    # be pruned
    mocker.patch(
        'pgtrigger.Protect.name',
        new_callable=mocker.PropertyMock,
        return_value='hi',
    )
    pgtrigger.prune()
    pgtrigger.prune()
    deletion_protected_model.delete()
