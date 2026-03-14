# Weather

Uses [Open-Meteo](https://open-meteo.com/) geocoding and weather API — no API key required.

## Configuration

```yaml
weather:
  image: executor-weather:latest
  args:
    location: denver   # city name or coordinates
```

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `location` | yes | City name or coordinates (e.g., `denver`, `39.7,-104.9`) |
