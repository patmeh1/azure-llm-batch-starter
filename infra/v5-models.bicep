targetScope = 'resourceGroup'

@description('Existing approved AI Services account. Rebalances only the named GPT-5 mini control and adds one target.')
param accountName string

@description('Read from the existing control deployment; preserve its content policy unchanged.')
param raiPolicyName string

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource control 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'gpt5-mini-online'
  sku: {
    name: 'DataZoneStandard'
    capacity: 100
  }
  properties: {
    raiPolicyName: raiPolicyName
    model: {
      format: 'OpenAI'
      name: 'gpt-5-mini'
      version: '2025-08-07'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource fifth 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: 'gpt5-mini-v5-5'
  sku: {
    name: 'DataZoneStandard'
    capacity: 50
  }
  properties: {
    raiPolicyName: raiPolicyName
    model: {
      format: 'OpenAI'
      name: 'gpt-5-mini'
      version: '2025-08-07'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  dependsOn: [control]
}

output allocationNote string = '100K control + three retained 50K targets + new 50K target = 300K TPM'
