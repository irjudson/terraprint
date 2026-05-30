---
title: "Terraprint: From Drone Flight to 3D-Printed Ranch in One Pipeline"
date: 2026-06-03 09:00:00 -0600
categories: [Projects, Hardware]
tags: [drones, photogrammetry, 3d-printing, gis, dji, opendronemap, terrain, agtech, buffalo-jump-forge]
---

# Terraprint: From Drone Flight to 3D-Printed Ranch in One Pipeline

I run cattle on a piece of ground near Three Forks, Montana called Buffalo Jump Ranch. It is a rolling, awkward shape of land that does not survey well in your head. I wanted a physical model of it on my desk — something I could point at when a neighbor asked about a fence line, or when I was planning where to put a new tank.

The obvious answer was 3D printing. The non-obvious answer was the toolchain. Six months in, what started as a weekend GeoTIFF-to-STL script has turned into Terraprint: an open pipeline that takes you from "draw a polygon on a map" to a watertight, printer-ready terrain model, with a drone-based photogrammetry path for places the public DEMs do not see clearly enough.

This is what it does, how it is built, and the few decisions I made along the way that I think were worth making.

## The Problem with Public Elevation Data

USGS 3DEP and Google Earth Engine's SRTM/NED layers will get you a long way. For most of the lower 48 you can pull a 1-meter or 10-meter DEM, hand it to a slicer, and print it. That is exactly what MVP 0 of Terraprint does: you give it a place name or a bounding box, it pulls tiles via TouchTerrain, splits them to your printer bed, and emits watertight STLs.

For my Anycubic Kobra 3 Max — 400x400mm bed — the tiling math has to know about the bed, the model scale, and the seam between tiles. The pipeline handles that with printer profiles in `configs/`.

But once you start caring about features smaller than the DEM resolution — a new outbuilding, an eroded coulee, the exact line of a two-track — public data falls apart. You can see the hill. You cannot see the barn. That is what pushed the project into the drone half.

## Architecture in Two Halves

Terraprint is two MVPs glued together by a Makefile and a Docker Compose file.

The terrain side is a Python pipeline that wraps TouchTerrain, GDAL, and a small STL post-processor. Everything runs in containers, so I do not have to fight a local GDAL install every time Ubuntu updates. Inputs can be a place name, a bounding box, or a GeoTIFF you bring yourself. Outputs are STLs tiled to a configured printer.

The drone side is a Progressive Web App. You open it on a phone or a laptop, draw a polygon on a satellite basemap, set altitude and overlap, and it computes a lawnmower flight path. It previews the path, estimates battery use, and then — this is the part I am most proud of — pushes the mission directly to the DJI flight app on a tethered iPhone over USB.

```
[ Map / polygon ]  ->  [ Path planner ]  ->  [ WPML generator ]
                                                     |
                                                     v
                                              [ pymobiledevice3 ]
                                                     |
                                                     v
                                              [ Skyrover X1 iOS ]
                                                     |
                                                     v
                                              [ Drone flies it ]
                                                     |
                                                     v
                                              [ photos / SRT ]
                                                     |
                                                     v
                                              [ OpenDroneMap ]
                                                     |
                                                     v
                                              [ DSM -> STL ]
```

## Why Push Missions Over USB

The conventional path is: plan in DJI's web tool, export, email it to yourself, open the app, import. That works at a desk. It does not work standing in a pasture at 5:30 AM with one bar of LTE.

DJI's mobile apps store waypoint missions in a SQLite database inside the app sandbox. On iOS, that sandbox is reachable via AFC (Apple File Conduit) if you have a trusted USB connection. `pymobiledevice3` speaks that protocol from Python.

So the planner generates a KMZ containing a WPML mission file, opens an AFC session to the iPhone, drops the KMZ into the app's documents directory, and inserts a row into its mission database. When you open the app, the mission is just there. No cloud, no Wi-Fi, no QR codes.

This is the kind of integration that feels illegal until you read the docs. It is not. It is the same path the DJI app itself uses when you "import" a mission. We are just skipping the human in the middle.

## WPML and the Gimbal Trick

DJI's Waypoint Mission Markup Language is XML. Each waypoint can carry an action group, and each action group is a list of actions executed in order at that waypoint. The two I care about are `gimbalRotate` and `takePhoto`.

For a standard nadir mission, you set the gimbal to -90 degrees once at the start and forget about it. For photogrammetry, that is not enough. Modern photogrammetry pipelines — OpenDroneMap, RealityCapture, Metashape — reconstruct much more accurate models if you also fly oblique passes at roughly 45 degrees. The classic recipe is five passes: one nadir, and four obliques pointed north, east, south, and west.

The trap is that DJI will happily fly all five missions with the gimbal stuck wherever you last left it. You have to set the gimbal angle explicitly at every single waypoint, before every photo. So Terraprint emits action groups that look like this:

```xml
<wpml:actionGroup>
  <wpml:actionGroupId>1</wpml:actionGroupId>
  <wpml:actionGroupStartIndex>0</wpml:actionGroupStartIndex>
  <wpml:actionGroupEndIndex>0</wpml:actionGroupEndIndex>
  <wpml:actionGroupMode>sequence</wpml:actionGroupMode>
  <wpml:actionTrigger>
    <wpml:actionTriggerType>reachPoint</wpml:actionTriggerType>
  </wpml:actionTrigger>
  <wpml:action>
    <wpml:actionId>0</wpml:actionId>
    <wpml:actionActuatorFunc>gimbalRotate</wpml:actionActuatorFunc>
    <wpml:actionActuatorFuncParam>
      <wpml:gimbalPitchRotateAngle>-45</wpml:gimbalPitchRotateAngle>
      <wpml:gimbalYawRotateAngle>0</wpml:gimbalYawRotateAngle>
      <wpml:gimbalRotateMode>absoluteAngle</wpml:gimbalRotateMode>
    </wpml:actionActuatorFuncParam>
  </wpml:action>
  <wpml:action>
    <wpml:actionId>1</wpml:actionId>
    <wpml:actionActuatorFunc>takePhoto</wpml:actionActuatorFunc>
  </wpml:action>
</wpml:actionGroup>
```

Two actions per waypoint: set the gimbal, then shoot. The oblique passes also set `waypointHeadingMode` to `fixed` with compass bearings of 0, 90, 180, and 270 degrees, so the drone is always pointed in the direction the camera is looking. The nadir pass uses `followWayline` and a pitch of -90.

When you trigger photogrammetry mode in the planner, it generates all five KMZs and pushes them to the phone in a single USB session. You walk out to the drone, fly them in order, and come back with a coherent set of images.

## How Long Does It Actually Take

This is the question nobody answers in tutorials, so I will.

At 80m altitude with 80% overlap, the planner reports 4,316 waypoints per pass over the core ranch area (roughly 50 acres around the main buildings). Each pass takes about 15 minutes of flight time — well within one battery. Five passes, five batteries, about 90 minutes from wheels-up to SD card in hand. Three minutes to swap batteries between passes, maybe ten minutes to reposition between the nadir and each cardinal oblique. Call it two hours with setup and breakdown.

For the full 640-acre property the math is less friendly: 173 minutes of flight per pass, seven batteries per pass, 35 battery swaps across all five passes, and about 16 hours of total clock time spread across several days. That is a real project, not a weekend afternoon. Start with a smaller test area.

## The Demo Workflow

End to end, on a flight day, this is the loop:

```bash
# Plan and push (from the web UI at http://localhost:8001)
docker compose up web

# Fly the five missions from the Skyrover X1 app, swap SD card

# Reconstruct
make odm FLIGHT=buffalo_jump_ranch

# Make terrain STLs from the resulting DSM
make terrain FLIGHT=buffalo_jump_ranch
```

`make odm` runs OpenDroneMap in its official container against the flight folder and produces an orthomosaic, a DSM, and a point cloud. `make terrain` takes the DSM, reprojects it, applies the active printer profile, and writes tiled STLs into `data/out/`.

The whole thing is covered by a small but real test suite — 18 pytest cases covering footprint math, lawnmower grid geometry, and WPML encoding. I would rather have a test that catches a wrong gimbal angle than discover it on the SD card after a 20-minute flight.

## What I Learned Building It

A few things that surprised me:

The hardest part was not the GIS or the 3D printing. It was the seam between the planner and the drone. Every vendor's mission format is just different enough to be annoying, and every iOS app sandbox is just locked down enough to make you think you cannot get in. You can, you just have to read more docs than is reasonable.

Photogrammetry quality is dominated by flight plan, not post-processing. Five passes at decent overlap will beat one perfect nadir pass at 90 percent overlap every time. Once I accepted that and put the effort into the gimbal-per-waypoint encoding, the OpenDroneMap output stopped being mushy.

Containerizing GDAL was worth every minute. I have not edited a system Python path in months.

## What Is Next

A few things I am actively working on:

MVP 2 is building extraction. If you subtract a DTM from a DSM, what remains above ground level is, mostly, buildings and trees. With a little classification you can pull individual structures out and print them as separate models. I want a barn-shaped magnet on my fridge.

MVP 3 is snap-fit assembly. Pockets for round magnets at tile boundaries, so a large ranch prints as nine tiles that click together.

MVP 5 is where it gets fun — driving OrcaSlicer from the command line so the whole pipeline ends at a gcode file, not an STL.

## Try It

Terraprint is open source. If you fly a DJI drone, run a 3D printer, or just want a model of the hill behind your house, it should work for you out of the box on Linux or macOS with Docker installed.

Repo: [github.com/irjudson/terraprint](https://github.com/irjudson/terraprint)

If you try it on land I have not flown, I want to see the print. If you find a bug in the WPML encoder before your drone does, even better — open an issue.
