#!/usr/bin/env python3
"""Generate/check/apply the opt-in SocketStream patch to an isolated Mutter 50.1.

Default is read-only. All edits are prepared before git apply atomically checks
and applies the diff. No package installation or running-compositor changes.
Reference: GNOME/mutter 50.1, commit 8e9bb806da4a6a4fc87aa4a481b228dba6735cbd.
"""
from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import re
import subprocess
import sys

FLAG = "CLUTTER_PAINT_FLAG_SOCKET_STREAM_CAPTURE"
ENABLED = 'g_strcmp0 (g_getenv ("SOCKET_STREAM_CAPTURE_EXCLUDE"), "1") == 0'


class PatchError(RuntimeError):
    pass


def prepare(root: Path) -> str:
    """Validate every anchor, then return a unified diff without writing files."""
    version = (root / "meson.build").read_text(encoding="utf-8")
    if not re.search(r"version:\s*'50\.1'", version):
        raise PatchError("expected an isolated Mutter 50.1 source checkout")
    originals: dict[str, str] = {}
    changed: dict[str, str] = {}

    def change(path: str, old: str, new: str, count: int = 1):
        if path not in originals:
            originals[path] = (root / path).read_text(encoding="utf-8")
            changed[path] = originals[path]
        text = changed[path]
        if text.count(new) == count:
            return
        if text.count(old) != count:
            raise PatchError(f"{path}: expected {count} compatible anchors; use a fresh 50.1 tree")
        changed[path] = text.replace(old, new)

    change("clutter/clutter/clutter-paint-context.h",
           "  CLUTTER_PAINT_FLAG_CLEAR = 1 << 2,",
           f"  CLUTTER_PAINT_FLAG_CLEAR = 1 << 2,\n  {FLAG} = 1 << 3,")

    # A private instance callback avoids changing ClutterActorClass's ABI.
    # Check before effects/paint nodes, so a cached effect on the window itself
    # cannot substitute its previously rendered contents for a filtered actor.
    change("clutter/clutter/clutter-actor.h",
           "CLUTTER_EXPORT\nClutterActor *                  clutter_actor_new                               (void);",
           "CLUTTER_EXPORT\nClutterActor *                  clutter_actor_new                               (void);\n\n"
           "CLUTTER_EXPORT\nvoid clutter_actor_set_capture_filter (ClutterActor *self,\n"
           "                                       gboolean (*filter) (ClutterActor *actor));")
    actor = "clutter/clutter/clutter-actor.c"
    change(actor, "struct _ClutterActorPrivate\n{",
           "struct _ClutterActorPrivate\n{\n  gboolean (*capture_filter) (ClutterActor *actor);")
    change(actor, "/**\n * clutter_actor_paint:\n",
           "/* Internal compositor predicate, evaluated only for capture paints. */\n"
           "void\nclutter_actor_set_capture_filter (ClutterActor *self,\n"
           "                                  gboolean (*filter) (ClutterActor *actor))\n"
           "{\n  g_return_if_fail (CLUTTER_IS_ACTOR (self));\n"
            "  self->priv->capture_filter = filter;\n"
            "  clutter_actor_queue_redraw (self);\n}\n\n/**\n * clutter_actor_paint:\n")
    change(actor, "  priv = self->priv;\n  priv->propagated_one_redraw = FALSE;",
           "  priv = self->priv;\n"
           f"  if ((clutter_paint_context_get_paint_flags (paint_context) & {FLAG}) &&\n"
           "      priv->capture_filter && priv->capture_filter (self))\n    return;\n\n"
           "  priv->propagated_one_redraw = FALSE;")

    # Parent effects cache whole subtrees. Never reuse a local cache in capture
    # or a capture cache locally. Refresh every capture: predicates can depend
    # on mutable window identity or ancestry without a local paint in between.
    effect = "clutter/clutter/clutter-offscreen-effect.c"
    change(effect, "typedef struct _ClutterOffscreenEffectPrivate\n{",
           "typedef struct _ClutterOffscreenEffectPrivate\n{\n  gboolean capture_cache;")
    change(effect, "  if (flags & CLUTTER_EFFECT_PAINT_BYPASS_EFFECT)",
           f"  gboolean capture = !!(clutter_paint_context_get_paint_flags (paint_context) & {FLAG});\n\n"
            "  if (capture || priv->capture_cache != capture)\n"
           "    flags |= CLUTTER_EFFECT_PAINT_ACTOR_DIRTY;\n"
           "  priv->capture_cache = capture;\n\n"
            "  if (flags & CLUTTER_EFFECT_PAINT_BYPASS_EFFECT)")

    # Shell animations reuse local snapshots in ordinary St.Widget actors.
    # Keep their pixels locally, but carry exclusion on the content itself.
    texture = "clutter/clutter/clutter-texture-content.c"
    change(texture, '#include "clutter/clutter-paint-node.h"',
           '#include "clutter/clutter-paint-node.h"\n#include "clutter/clutter-paint-context.h"')
    change(texture, "  CoglTexture *texture;\n};",
           "  CoglTexture *texture;\n  gboolean exclude_from_capture;\n};")
    change(texture, "  node = clutter_actor_create_texture_paint_node (actor,",
           "  if (texture_content->exclude_from_capture &&\n"
           f"      (clutter_paint_context_get_paint_flags (paint_context) & {FLAG}))\n"
           "    return;\n\n  node = clutter_actor_create_texture_paint_node (actor,")
    setter = '''void
clutter_texture_content_set_capture_excluded (ClutterTextureContent *content,
                                               gboolean               excluded)
{
  g_return_if_fail (CLUTTER_IS_TEXTURE_CONTENT (content));
  content->exclude_from_capture = excluded;
  clutter_content_invalidate (CLUTTER_CONTENT (content));
}

'''
    change(texture, "/**\n * clutter_texture_content_get_texture:",
           setter + "/**\n * clutter_texture_content_get_texture:")
    change("clutter/clutter/clutter-texture-content.h", "G_END_DECLS",
           "CLUTTER_EXPORT\nvoid clutter_texture_content_set_capture_excluded (ClutterTextureContent *content,\n"
           "                                                       gboolean               excluded);\n\nG_END_DECLS")
    change("clutter/clutter/clutter-stage.c",
           "  return clutter_texture_content_new_from_texture (cogl_offscreen_get_texture (offscreen),\n"
           "                                                   NULL);",
           "  {\n    ClutterContent *content =\n"
           "      clutter_texture_content_new_from_texture (cogl_offscreen_get_texture (offscreen), NULL);\n\n"
           f"    if ({ENABLED})\n"
           "      clutter_texture_content_set_capture_excluded (CLUTTER_TEXTURE_CONTENT (content), TRUE);\n"
           "    return content;\n  }")

    # Only actual capture consumers set the flag. Generic offscreen APIs also
    # serve local Shell screen transitions and window animations.
    for source in ("monitor", "area"):
        change(f"src/backends/meta-screen-cast-{source}-stream-src.c",
               "ClutterPaintFlag paint_flags = CLUTTER_PAINT_FLAG_CLEAR;",
               f"ClutterPaintFlag paint_flags = CLUTTER_PAINT_FLAG_CLEAR | {FLAG};", count=2)
    change("src/compositor/meta-window-group.c",
           "  redraw_clip = clutter_paint_context_get_redraw_clip (paint_context);",
           f"  if ((clutter_paint_context_get_paint_flags (paint_context) & {FLAG}) &&\n"
           f"      {ENABLED})\n"
           "    {\n"
           "      /* A locally opaque panel must not clip the scene behind it. */\n"
           "      meta_cullable_cull_redraw_clip (META_CULLABLE (window_group), NULL);\n"
           "      parent_actor_class->paint (actor, paint_context);\n      return;\n    }\n\n"
           "  redraw_clip = clutter_paint_context_get_redraw_clip (paint_context);")

    # Keep surfaces behind an opaque excluded panel receiving frame callbacks
    # and scheduling damage. Otherwise they can freeze in the shared scene.
    for group in ("window_group", "top_window_group"):
        change("src/compositor/compositor.c",
               f"  meta_cullable_cull_unobscured (META_CULLABLE (priv->{group}), unobscured_region);",
               f"  meta_cullable_cull_unobscured (META_CULLABLE (priv->{group}),\n"
               f"                                 ({ENABLED}) ? NULL : unobscured_region);")

    window = "src/compositor/meta-window-actor.c"
    predicate = f'''static gboolean
socket_stream_excludes_actor (ClutterActor *actor)
{{
  MetaWindow *window =
    meta_window_actor_get_meta_window (META_WINDOW_ACTOR (actor));
  unsigned int depth;

  if (!({ENABLED}))
    return FALSE;

  /* WM_CLASS on XWayland; app_id is exposed as wm-class on native Wayland.
   * Include transient children, without trusting their title or process name. */
  for (depth = 0; window && depth < 64; depth++)
    {{
      if (g_strcmp0 (meta_window_get_wm_class (window), "socket_stream_overlay") == 0)
        return TRUE;
      window = meta_window_get_transient_for (window);
    }}

  return FALSE;
}}

'''
    change(window, "static void\nmeta_window_actor_class_init (MetaWindowActorClass *klass)",
           predicate + "static void\nmeta_window_actor_class_init (MetaWindowActorClass *klass)")
    change(window, "  priv->surface_actors = g_ptr_array_new ();",
           "  clutter_actor_set_capture_filter (CLUTTER_ACTOR (self),\n"
           "                                    socket_stream_excludes_actor);\n\n"
           "  priv->surface_actors = g_ptr_array_new ();")
    # A direct single-window SHM stream can bypass the actor renderer entirely.
    change(window, "  image = meta_window_actor_get_image (window_actor, bounds);",
           "  if (socket_stream_excludes_actor (CLUTTER_ACTOR (window_actor)))\n"
           "    {\n"
           "      memset (data, 0, (size_t) bounds->width * bounds->height * 4);\n"
           "      return;\n    }\n\n"
           "  image = meta_window_actor_get_image (window_actor, bounds);")
    # DMA-BUF window painting is capture; the shared image constructor accepts
    # provenance explicitly so local window animations retain their content.
    for prefix, flag in (
        ("  cogl_framebuffer_transform (framebuffer, &inverted_transform);", FLAG),
        ("  cogl_framebuffer_translate (framebuffer, -clip->x, -clip->y, 0);", "paint_flags"),
    ):
        anchor = (prefix + "\n\n  paint_context =\n"
                  "    clutter_paint_context_new_for_framebuffer (framebuffer, NULL,\n"
                  "                                               CLUTTER_PAINT_FLAG_NONE,")
        change(window, anchor, anchor.replace("CLUTTER_PAINT_FLAG_NONE", flag))
    change(window,
           "create_framebuffer_from_window_actor (MetaWindowActor  *self,\n"
           "                                      MtkRectangle     *clip,\n"
           "                                      GError          **error)",
           "create_framebuffer_from_window_actor (MetaWindowActor  *self,\n"
           "                                      MtkRectangle     *clip,\n"
           "                                      ClutterPaintFlag  paint_flags,\n"
           "                                      GError          **error)")
    for error_arg, flag in (("NULL", FLAG), ("error", "CLUTTER_PAINT_FLAG_NONE")):
        change(window,
               "  framebuffer = create_framebuffer_from_window_actor (self,\n"
               "                                                      &framebuffer_clip,\n"
               f"                                                      {error_arg});",
               "  framebuffer = create_framebuffer_from_window_actor (self,\n"
               "                                                      &framebuffer_clip,\n"
               f"                                                      {flag},\n"
               f"                                                      {error_arg});")
    change(window, "  if (!meta_shaped_texture_should_get_via_offscreen (stex) &&",
           "  if (!socket_stream_excludes_actor (actor) &&\n"
            "      !meta_shaped_texture_should_get_via_offscreen (stex) &&")
    change(window, "  content = clutter_texture_content_new_from_texture (texture, NULL);",
           "  content = clutter_texture_content_new_from_texture (texture, NULL);\n"
           "  clutter_texture_content_set_capture_excluded (CLUTTER_TEXTURE_CONTENT (content),\n"
           "                                                socket_stream_excludes_actor (actor));")

    monitor = "src/backends/meta-screen-cast-monitor-stream-src.c"
    change(monitor,
           "  if (paint_phase == META_SCREEN_CAST_PAINT_PHASE_DETACHED)\n    goto stage_paint;",
           "  /* Display-buffer and direct-scanout copies already contain the panel. */\n"
           f"  if ({ENABLED} ||\n"
           "      paint_phase == META_SCREEN_CAST_PAINT_PHASE_DETACHED)\n    goto stage_paint;")

    virtual = "src/backends/meta-screen-cast-virtual-stream-src.c"
    change(virtual, "  paint_flags = CLUTTER_PAINT_FLAG_CLEAR;",
           f"  paint_flags = CLUTTER_PAINT_FLAG_CLEAR | {FLAG};")
    change(virtual,
           "  view_framebuffer = clutter_stage_view_get_framebuffer (view);",
           f'''  if ({ENABLED})
    {{
      MetaScreenCastStream *stream = meta_screen_cast_stream_src_get_stream (src);
      MtkRectangle layout;
      ClutterPaintFlag flags = CLUTTER_PAINT_FLAG_CLEAR | {FLAG};

      clutter_stage_view_get_layout (view, &layout);
      switch (meta_screen_cast_stream_get_cursor_mode (stream))
        {{
        case META_SCREEN_CAST_CURSOR_MODE_METADATA:
        case META_SCREEN_CAST_CURSOR_MODE_HIDDEN:
          flags |= CLUTTER_PAINT_FLAG_NO_CURSORS;
          break;
        case META_SCREEN_CAST_CURSOR_MODE_EMBEDDED:
          flags |= CLUTTER_PAINT_FLAG_FORCE_CURSORS;
          break;
        }}
      clutter_stage_paint_to_framebuffer (stage_from_src (src), framebuffer,
                                          &layout,
                                          clutter_stage_view_get_scale (view),
                                          NULL, flags);
      return TRUE;
    }}

  view_framebuffer = clutter_stage_view_get_framebuffer (view);''')

    change("src/tests/clutter/conform/meson.build",
           "clutter_conform_tests_actor_tests = [",
           "clutter_conform_tests_actor_tests = [\n  'socket_stream_capture',")
    test_path = "src/tests/clutter/conform/socket_stream_capture.c"
    test_source = Path(__file__).with_name("socket_stream_capture.c").read_text(encoding="utf-8")
    existing = (root / test_path).read_text(encoding="utf-8") if (root / test_path).exists() else ""
    if existing and existing != test_source:
        raise PatchError(f"{test_path}: existing test differs; refusing to overwrite")
    originals[test_path] = existing
    changed[test_path] = test_source

    return "".join(
        "".join(difflib.unified_diff(originals[path].splitlines(keepends=True),
                                     text.splitlines(keepends=True),
                                     fromfile=f"a/{path}" if originals[path] else "/dev/null",
                                     tofile=f"b/{path}"))
        for path, text in changed.items() if text != originals[path]
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="apply after validating every edit")
    action.add_argument("--diff", action="store_true", help="emit the patch; do not modify sources")
    args = parser.parse_args(argv)
    try:
        root = args.source.resolve(strict=True)
        diff = prepare(root)
        if args.diff:
            print(diff, end="")
        elif args.apply and diff:
            for extra in (["--check"], []):
                subprocess.run(["git", "apply", *extra, "-"], cwd=root,
                               input=diff, text=True, check=True)
            print("Applied experimental capture filter; Linux build/runtime QA still required.")
        else:
            print("Compatible source; no files changed." if diff else "Patch already applied; no files changed.")
        return 0
    except (OSError, UnicodeError, PatchError, subprocess.CalledProcessError) as exc:
        print(f"capture patch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
