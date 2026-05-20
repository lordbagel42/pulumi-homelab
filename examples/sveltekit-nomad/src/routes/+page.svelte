<script lang="ts">
  const stack = import.meta.env.PUBLIC_SST_STAGE ?? "dev";
</script>

<main>
  <h1>SvelteKit on Nomad</h1>
  <p>Stage: <strong>{stack}</strong></p>
  <p>Running on Consul + Nomad, proxied by Traefik, protected by Authentik.</p>
</main>

<style>
  main {
    font-family: system-ui, sans-serif;
    max-width: 600px;
    margin: 4rem auto;
    padding: 0 1rem;
  }
</style>
