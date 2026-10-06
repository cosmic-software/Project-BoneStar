# Future ideas

Ideas that have been talked through but not started. Each one is written up so it can be
picked up later without going back over the design.

## 8K Moon near the camera (detail tiles)

**Why:** the whole-globe 4x Moon (`moon_4x.glb`, 248 MB, 4.2 M triangles, 8192 x 4096
colour and normal maps) is near WebVerse's limit: it crashed once while loading and loaded
on a retry. Full 8K detail is only needed on the part of the Moon close to the camera.

**Plan:**

- **Base:** the 1x Moon is always loaded, split into its 96 tiles as separate models so the
  viewer can hide one tile at a time.
- **Detail tiles:** `tools/make_moon.py` gains a step that cuts the 4x Moon into the same 96
  tiles (30 deg x 22.5 deg), each a small `.glb` of a few MB with its own crop of the 8K
  colour and normal maps. The polar caps are cut from the 4096 x 4096 polar maps the same way.
- **Streaming:** the viewer finds the point under the camera. It loads the detail tiles within
  the chosen radius and hides the base tiles beneath them. Moving away deletes those detail
  tiles and shows the base tiles again. This only happens when the camera is near the Moon.
- **Radius (to decide):** the tile under the camera alone, or that tile plus one ring around
  it (about 9 tiles, roughly 90 x 70 deg at the equator, around 25 MB loaded at once). The
  ring is the suggested choice.
- **Seams:** 1x has fewer vertices along each tile edge, so cracks can show where a detail
  tile meets a base tile. Hide them with a short skirt hanging down from each tile's edge.
- **Picker:** the Moon picker gets a "base + 8K near camera" option. The whole-globe 4x stays
  available.
