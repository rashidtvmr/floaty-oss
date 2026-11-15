/* SPDX-License-Identifier: GPL-2.0-or-later
 * Pixel regression for the capture-only actor filter, built by Mutter's
 * existing headless Clutter test harness. No desktop contents are captured.
 */
#include <clutter/clutter.h>
#include <clutter/clutter-backend.h>
#include <clutter/clutter-texture-content.h>

#include "tests/clutter-test-utils.h"

static gboolean exclude_enabled = TRUE;

static gboolean
exclude_actor (ClutterActor *actor)
{
  return exclude_enabled;
}

static void
on_presented (ClutterStage     *stage,
              ClutterStageView *view,
              ClutterFrameInfo *info,
              gboolean         *presented)
{
  *presented = TRUE;
}

static void
assert_pixel (CoglFramebuffer  *framebuffer,
              ClutterActor     *group,
              ClutterPaintFlag  flags,
              gboolean          red)
{
  ClutterPaintContext *paint_context;
  CoglColor clear = { 0, 0, 0, 255 };
  uint8_t pixel[4];

  cogl_framebuffer_clear (framebuffer, COGL_BUFFER_BIT_COLOR, &clear);
  cogl_framebuffer_orthographic (framebuffer, 0, 0, 128, 128, 0, 1);
  paint_context = clutter_paint_context_new_for_framebuffer (
    framebuffer, NULL, flags, clutter_actor_get_color_state (group));
  clutter_actor_paint (group, paint_context);
  clutter_paint_context_destroy (paint_context);
  cogl_framebuffer_read_pixels (framebuffer, 48, 48, 1, 1,
                                COGL_PIXEL_FORMAT_RGBA_8888, pixel);
  g_assert_cmpint (pixel[0], ==, red ? 255 : 0);
  g_assert_cmpint (pixel[1], ==, red ? 0 : 255);
  g_assert_cmpint (pixel[2], ==, 0);
}

static void
capture_filter_pixels (void)
{
  ClutterActor *stage = clutter_test_get_stage ();
  ClutterActor *group = clutter_actor_new ();
  ClutterActor *panel = clutter_actor_new ();
  ClutterActor *snapshot_actor;
  g_autoptr (ClutterContent) snapshot = NULL;
  MtkRectangle snapshot_rect = { 0, 0, 128, 128 };
  CoglContext *context =
    clutter_backend_get_cogl_context (clutter_test_get_backend ());
  g_autoptr (CoglTexture) texture = NULL;
  g_autoptr (CoglOffscreen) offscreen = NULL;
  CoglFramebuffer *framebuffer;
  g_autoptr (GError) error = NULL;
  CoglColor green = { 0, 255, 0, 255 };
  CoglColor red = { 255, 0, 0, 255 };
  gboolean presented = FALSE;
  gulong handler;

  clutter_actor_set_size (group, 128, 128);
  clutter_actor_set_background_color (group, &green);
  clutter_actor_set_size (panel, 64, 64);
  clutter_actor_set_position (panel, 32, 32);
  clutter_actor_set_background_color (panel, &red);
  clutter_actor_set_capture_filter (panel, exclude_actor);
  clutter_actor_add_child (group, panel);
  clutter_actor_add_child (stage, group);
  handler = g_signal_connect (stage, "presented", G_CALLBACK (on_presented), &presented);
  clutter_actor_show (stage);
  while (!presented)
    g_main_context_iteration (NULL, TRUE);
  g_signal_handler_disconnect (stage, handler);

  texture = cogl_texture_2d_new_with_size (context, 128, 128);
  offscreen = cogl_offscreen_new_with_texture (texture);
  framebuffer = COGL_FRAMEBUFFER (offscreen);
  g_assert_true (cogl_framebuffer_allocate (framebuffer, &error));
  g_assert_no_error (error);

  /* A/B/A verifies actual pixels and that capture does not hide the actor. */
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  g_assert_true (clutter_actor_is_visible (panel));

  /* Prime a cached offscreen effect before the capture pass. */
  clutter_actor_set_offscreen_redirect (panel, CLUTTER_OFFSCREEN_REDIRECT_ALWAYS);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);

  /* An ancestor's cached texture must never cross the capture/local boundary. */
  clutter_actor_set_offscreen_redirect (group, CLUTTER_OFFSCREEN_REDIRECT_ALWAYS);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  clutter_actor_set_offscreen_redirect (group, CLUTTER_OFFSCREEN_REDIRECT_AUTOMATIC_FOR_OPACITY);
  clutter_actor_set_offscreen_redirect (group, CLUTTER_OFFSCREEN_REDIRECT_ALWAYS);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);

  /* No local paint between these: callback state and setter changes must
   * invalidate every ancestor capture cache, not just mode transitions. */
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  exclude_enabled = FALSE;
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, TRUE);
  exclude_enabled = TRUE;
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  clutter_actor_set_capture_filter (panel, NULL);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, TRUE);
  clutter_actor_set_capture_filter (panel, exclude_actor);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);

  /* Real stage snapshot, reused by an actor with no predicate (like Shell's
   * ScreenTransition widget). Red locally, live green behind it in capture. */
  snapshot = clutter_stage_paint_to_content (CLUTTER_STAGE (stage), &snapshot_rect,
                                            1.0f, NULL, CLUTTER_PAINT_FLAG_NONE, &error);
  g_assert_no_error (error);
  g_assert_nonnull (snapshot);
  snapshot_actor = clutter_actor_new ();
  clutter_actor_set_size (snapshot_actor, 128, 128);
  clutter_actor_set_content (snapshot_actor, snapshot);
  clutter_actor_add_child (group, snapshot_actor);
  clutter_actor_hide (panel);
  /* Wait for layout: an unallocated snapshot can silently paint nothing. */
  presented = FALSE;
  handler = g_signal_connect (stage, "presented", G_CALLBACK (on_presented), &presented);
  clutter_actor_queue_redraw (stage);
  while (!presented)
    g_main_context_iteration (NULL, TRUE);
  g_signal_handler_disconnect (stage, handler);
  g_assert_false (clutter_actor_is_visible (panel));
  /* The hidden live panel cannot supply these red pixels. */
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_NONE, TRUE);
  /* Negative control must expose exactly the same snapshot in capture. */
  clutter_texture_content_set_capture_excluded (CLUTTER_TEXTURE_CONTENT (snapshot), FALSE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, TRUE);
  clutter_texture_content_set_capture_excluded (CLUTTER_TEXTURE_CONTENT (snapshot), TRUE);
  assert_pixel (framebuffer, group, CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE, FALSE);
  clutter_actor_destroy (group);
}

int
main (int argc, char *argv[])
{
  g_setenv ("SOCKET_STREAM_CAPTURE_EXCLUDE", "1", TRUE);
  clutter_test_init (&argc, &argv);
  clutter_test_add ("/socket_stream/capture/filter-pixels", capture_filter_pixels);
  return clutter_test_run ();
}
