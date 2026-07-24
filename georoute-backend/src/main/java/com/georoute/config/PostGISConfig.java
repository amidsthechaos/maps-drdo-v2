package com.georoute.config;

import org.locationtech.jts.geom.GeometryFactory;
import org.locationtech.jts.geom.PrecisionModel;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Spatial helpers. {@link org.springframework.jdbc.core.JdbcTemplate} is provided by
 * Spring Boot auto-configuration from the configured DataSource; the spatial reads and
 * writes (snap query, graph load, SHP ingest) use it with native PostGIS SQL.
 */
@Configuration
public class PostGISConfig {

    /** Shared WGS84 (EPSG:4326) geometry factory. */
    @Bean
    public GeometryFactory geometryFactory() {
        return new GeometryFactory(new PrecisionModel(), 4326);
    }
}
