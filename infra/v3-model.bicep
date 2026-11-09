targetScope = 'resourceGroup'

@description('Existing AI Services account in an approved US region.')
param accountName string

@description('Separate deployment name; do not overwrite existing model deployments.')
param deploymentName string = 'gpt5-mini-online'

@minValue(1)
@maxValue(300)
param capacity int = 150

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: deploymentName
  sku: {
    name: 'DataZoneStandard'
    capacity: capacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5-mini'
      version: '2025-08-07'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

output deploymentId string = deployment.id
output onlineDeployment string = deployment.name
