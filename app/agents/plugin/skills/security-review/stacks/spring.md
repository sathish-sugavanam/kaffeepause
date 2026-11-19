# Spring pack

- **SQL and JPQL.** String-concatenated `createQuery`, `createNativeQuery` or `JdbcTemplate` calls. Use bind parameters. `@Query` with SpEL on user input.
- **Authorisation.** New controller methods missing `@PreAuthorize` or `@Secured`, or not covered by the `SecurityFilterChain` matchers. A `permitAll()` added to a broad matcher.
- **Object scoping.** `repository.findById(id)` without checking the owner against the principal.
- **Mass assignment.** Binding request bodies straight onto JPA entities lets callers set `role`, `id` or `owner`. Use DTOs.
- **CSRF.** `csrf().disable()` (or `csrf(AbstractHttpConfigurer::disable)`) for an app with cookie sessions.
- **Deserialisation.** `ObjectInputStream.readObject`, Jackson `enableDefaultTyping` or `@JsonTypeInfo(use = Id.CLASS)`, XStream without an allowlist.
- **XXE.** `DocumentBuilderFactory`, `SAXParserFactory` or `XMLInputFactory` without disabling external entities.
- **SSRF and paths.** `RestTemplate`/`WebClient` to user URLs; `Paths.get(base, userInput)` without `normalize()` and a prefix check.
- **Actuator.** New actuator endpoints exposed (`management.endpoints.web.exposure.include=*`) without auth.
- **Logging.** User input in log messages (log forging), and secrets or tokens logged.
