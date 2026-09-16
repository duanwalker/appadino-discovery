@description('Location for the Container Registry')
param location string

@description('Short prefix used to build resource names (alphanumeric only — ACR names can\'t contain hyphens)')
param namePrefix string

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: '${namePrefix}acr'
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

output name string = acr.name
output loginServer string = acr.properties.loginServer
output id string = acr.id
