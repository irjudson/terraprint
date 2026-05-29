---
publish_date: 2026-06-04
platform: LinkedIn
---

**From Drone Flight to 3D-Printed Ranch: Building Terraprint**

Last weekend I held a palm-sized version of our ranch in my hand.

Every ridge, every coulee, the slope down to the Madison — printed in PLA, accurate to a few feet. I made it by flying a drone over the property, pushing the photos through an open-source pipeline I've been building, and sending the result to my 3D printer.

The project is called Terraprint, and it started with a simple itch. I live near Three Forks, Montana, on a place we call Buffalo Jump Ranch. I wanted a physical model of the land — something you can put on a table and point at when you're talking about water, fences, or where the elk bed down. Topo maps are fine. A 3D print is something else entirely.

The first version was straightforward. Give it a place name or a bounding box, and it pulls elevation data from USGS and Google Earth Engine, tiles it, makes the meshes watertight, and spits out STL files ready to slice. Docker-based, so it runs the same on my laptop as on a server. That got me printable terrain for anywhere in the US in about ten minutes.

But public elevation data tops out around one meter per pixel — fine for mountains, blurry for the details that actually make a place feel like itself. The barn doesn't show up. Neither does the cut bank along the creek. To get there, I needed my own data. Which meant flying the drone properly.

So the second piece is a mission planner — a PWA where you draw a polygon on a satellite map, set your altitude and overlap, and it generates a DJI waypoint mission. The fun part: it pushes the mission straight to the iPhone over USB using pymobiledevice3, so the DJI Fly app picks it up without any cloud round-trip. No accounts, no uploads, no waiting on someone else's servers.

The piece I'm most proud of is photogrammetry mode. A standard nadir grid (camera pointing straight down) gives you a great DSM but lousy 3D — vertical surfaces basically disappear. So the planner now generates a five-pass mission: one nadir grid plus four oblique passes at 45 degrees from each compass direction. All five missions push to the phone in a single tap. You fly them, land, and you have the photo set needed for a real 3D reconstruction.

From there, OpenDroneMap chews through the images and produces a DSM, which feeds back into the same STL pipeline. Drone takes off, drone lands, model comes out of the printer. The loop is closed.

One thing tutorials never tell you: how long it actually takes. For a 50-acre test area at 80m altitude, the five passes take about 90 minutes total — one battery per pass, three minutes to swap, done before lunch. For the full 640-acre ranch, the math is less friendly: 35 battery swaps, roughly 16 hours of flying spread across several days. Start small, prove the pipeline, then scale up.

A few things I learned worth sharing. Self-hosted matters more than I expected — owning the whole pipeline means I can fly a property without anyone's terms of service in the way. USB control of the iPhone is a wildly underused trick; it's faster and more reliable than anything wireless. And there is a real appetite among ranchers and small operators for tools that treat their land as the unit of analysis, not someone else's grid square.

Terraprint is open source. If you're building in drones, AgTech, mapping, or you just want a tiny replica of the place you call home, I'd love feedback — and especially war stories from anyone who has wrestled with DJI's mission formats, ODM tuning, or printing weird organic geometry.

Repo: https://github.com/irjudson/terraprint

#Photogrammetry #AgTech #OpenSource
