using 'main.bicep'

param location = 'eastus2'
param namePrefix = 'adisc-dev'
param postgresAdminLogin = 'discoveryadmin'
param keyVaultAdminPrincipalId = '053a555a-2d92-4234-a24b-54c6ad64d668'

// Read from an environment variable at deploy time — never committed.
// PowerShell: $env:PG_ADMIN_PW = <generated password>
param postgresAdminPassword = readEnvironmentVariable('PG_ADMIN_PW')
