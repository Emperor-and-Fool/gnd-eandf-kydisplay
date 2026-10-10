/* The stream helper is found on D-Bus by the unit it was started as.
 *
 * Kydisplay reads a helper's state through its D-Bus name, so the name has to
 * follow from the unit name alone, and the daemon has to hand that unit name
 * to the helper whether systemd starts it or the daemon does. */
#include <glib.h>

#include "nd-systemd-helpers.h"
#include "stream/nd-stream.h"

#define CONNECTION "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
#define UNIT "gnome-network-displays-stream-11111111-2222-3333-4444-555555555555-" CONNECTION ".service"

#define URI "gnome-network-displays://sink"

/* What the daemon puts in the helper's environment, as a string vector. */
static gchar **
stream_environment (GVariant *properties)
{
  GVariantIter iter;
  const gchar *key;
  GVariant *value;
  gchar **env = NULL;

  g_variant_iter_init (&iter, properties);
  while (g_variant_iter_next (&iter, "(&sv)", &key, &value))
    {
      if (g_str_equal (key, "Environment"))
        {
          g_clear_pointer (&env, g_strfreev);
          env = g_variant_dup_strv (value, NULL);
        }
      g_variant_unref (value);
    }

  return env;
}

int
main (void)
{
  g_autofree gchar *id = nd_stream_application_id (UNIT);
  g_autofree gchar *random_id = nd_stream_application_id (NULL);
  g_autofree gchar *bad_id = nd_stream_application_id ("something-else.service");
  g_autoptr(GVariant) plain = build_properties (UNIT, URI, "TV", FALSE, FALSE);
  g_autoptr(GVariant) asked = build_properties (UNIT, URI, "TV", TRUE, TRUE);
  g_auto(GStrv) plain_env = stream_environment (plain);
  g_auto(GStrv) asked_env = stream_environment (asked);

  g_assert_cmpstr (id, ==, ND_BUS_PREFIX ".Stream_0f1e2d3c_4b5a_6978_8796_a5b4c3d2e1f0");
  g_assert_true (g_application_id_is_valid (id));

  /* Without a unit the name stays random but valid, as upstream had it. */
  g_assert_true (g_str_has_prefix (random_id, ND_BUS_PREFIX ".Stream_"));
  g_assert_true (g_application_id_is_valid (random_id));
  g_assert_true (g_application_id_is_valid (bad_id));

  /* systemd passes the unit name on in the helper's environment. */
  g_assert_nonnull (plain_env);
  g_assert_true (g_strv_contains ((const gchar * const *) plain_env, "ND_STREAM_UNIT=" UNIT));

  /* What to share and what the link will carry travel the same way, because
   * both are read while the helper is being constructed, before it has been
   * handed the address it was started for (patches 0058 and 0060). Neither is
   * said unless it was asked for: a helper that found ND_STREAM_PICK set on
   * every cast would interrupt each one to ask the same question again. */
  g_assert_false (g_strv_contains ((const gchar * const *) plain_env, "ND_STREAM_PICK=new"));
  g_assert_false (g_strv_contains ((const gchar * const *) plain_env, "ND_STREAM_LINK=slow"));

  g_assert_nonnull (asked_env);
  g_assert_true (g_strv_contains ((const gchar * const *) asked_env, "ND_STREAM_UNIT=" UNIT));
  g_assert_true (g_strv_contains ((const gchar * const *) asked_env, "ND_STREAM_PICK=new"));
  g_assert_true (g_strv_contains ((const gchar * const *) asked_env, "ND_STREAM_LINK=slow"));

  return 0;
}
