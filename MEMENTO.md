# terraprint

A web-based mission planner for surveyors and field researchers that allows users to define survey areas, plan routes, and visualize terrain data with satellite imagery from Mapbox, Esri, and OpenTopoMap.

**Stack:** Python, FastAPI, Vue.js, Leaflet, Docker, Mapbox API, Esri API, USGS Topo

## Current Status
- The mission planner UI is fully functional with satellite layer cycling and topo overlays
- Mapbox token support is implemented via environment variables and API endpoints
- Docker configuration is ready for deployment

## Recent Decisions
- 2024-05-24: Added Mapbox token support via env var and API endpoint to enable satellite imagery
- 2024-05-24: Implemented cycling satellite layers through Mapbox → Esri → Topo modes
- 2024-05-24: Added favicon and updated README for better project presentation
- 2024-05-24: Integrated OSM street layers and USGS topo overlays for enhanced visualization
- 2024-05-24: Configured Docker setup with web service and uvicorn server

## Open Issues
- Mapbox satellite data is often 2-4 years old in rural areas
- Topo overlay currently fades satellite imagery too much

## Key Files
- `web/app.py`: FastAPI server with config endpoint and static file serving
- `web/static/index.html`: Main Vue.js frontend with Leaflet map and UI controls
- `web/static/main.js`: Vue app logic for map interactions, layer switching, and config fetching
- `web/static/manifest.json`: Web app manifest including favicon reference
- `docker-compose.yml`: Docker orchestration for web service with env var support
- `pyproject.toml`: Project dependencies and CLI entry points
- `.env.example`: Environment variable template including MAPBOX_TOKEN
- `README.md`: Project documentation with setup and usage instructions

## Recent Activity
- 2024-05-25: Updated satellite layer cycling logic and removed unused street layer
- 2024-05-24: Implemented favicon, env var config, Docker setup, and README updates
- 2024-05-24: Added Mapbox token support and satellite layer switching
- 2024-05-24: Integrated topo overlay and OSM layers for enhanced visualization
- 2024-05-24: Configured Docker and uvicorn server for web deployment
- 2024-05-24: Updated file layout and README to include web directory
- 2024-05-24: Added CLI entry point and project dependencies
- 2024-05-24: Implemented API config endpoint and static file serving
- 2024-05-24: Created favicon and updated manifest.json
- 2024-05-24: Added satellite layer cycling logic for Mapbox → Esri → Topo
