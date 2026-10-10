#include <gst/gst.h>

#include "wfd-media-factory.h"

/* The constrained-high video encoders of wfd_media_factory_profiles, in the
 * order that table lists them. Kept here rather than read from the table so
 * that reordering the table is a deliberate act that fails this test. */
static const gchar *encoder_preference[] = {
  "vah264lpenc",
  "nvh264enc",
  "x264enc",
  "vah264enc",
  "vaapih264enc",
};

static const gchar *
first_installed_encoder (void)
{
  for (guint i = 0; i < G_N_ELEMENTS (encoder_preference); i++)
    {
      g_autoptr(GstElementFactory) factory = gst_element_factory_find (encoder_preference[i]);

      if (factory)
        return encoder_preference[i];
    }

  return NULL;
}

static GstElement *
create_test_source (WfdMediaFactory *factory, gpointer user_data)
{
  GstElement *source;

  (void) factory;
  (void) user_data;
  source = gst_element_factory_make ("videotestsrc", NULL);
  gst_object_ref_sink (source);
  return gst_object_ref (source);
}

int
main (int argc, char **argv)
{
  g_autoptr(WfdMediaFactory) factory = NULL;
  g_autoptr(GstElement) pipeline = NULL;
  g_autoptr(GstElement) encoder = NULL;
  g_auto(GStrv) missing_video = NULL;
  g_auto(GStrv) missing_audio = NULL;
  GstElementFactory *element_factory;

  gst_init (&argc, &argv);
  factory = wfd_media_factory_new ();

  g_assert_true (wfd_media_factory_lookup_encoders (factory,
                                                     PROFILE_HIGH_H264,
                                                     &missing_video,
                                                     &missing_audio));

  g_signal_connect (factory,
                    "create-source",
                    G_CALLBACK (create_test_source),
                    NULL);
  pipeline = gst_pipeline_new ("encoder-selection-test");
  g_assert_nonnull (wfd_media_factory_create_video_element (factory,
                                                             GST_BIN (pipeline)));

  encoder = gst_bin_get_by_name (GST_BIN (pipeline), "wfd-encoder");
  g_assert_nonnull (encoder);
  element_factory = gst_element_get_factory (encoder);
  g_assert_nonnull (element_factory);

  /* The first encoder in the table that this machine actually has.
   *
   * Asserting a name outright would only say what the machine running the
   * test happens to be fitted with. What is worth pinning is the order,
   * because that order is a decision and not an accident:
   *
   *   - The part built into the processor comes first (patch 0045). It is
   *     already awake and already inside the laptop's thermal budget, where
   *     waking the card costs about fifteen watts out of the same vents.
   *   - The card comes next (patch 0039), which costs the desktop no
   *     processor time and does not queue behind a browser.
   *   - Software comes after both. Upstream of this project it came first
   *     (patch 0001), because stopping a cast once aborted inside the VA-API
   *     driver's own teardown. Later use did not reproduce that, and the two
   *     patches above were written on the strength of it; should the abort
   *     return, the order here is where it is answered.
   *
   * So this fails if the order changes, and passes on a machine with only
   * software encoding, which is the same statement in both places. */
  g_assert_cmpstr (gst_plugin_feature_get_name (GST_PLUGIN_FEATURE (element_factory)),
                   ==,
                   first_installed_encoder ());

  return 0;
}
