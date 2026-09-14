const SuiteCloudJestConfiguration = require("@oracle/suitecloud-unit-testing/jest-configuration/SuiteCloudJestConfiguration");

module.exports = {
  ...SuiteCloudJestConfiguration.build({
    projectFolder: "src",
    projectType: SuiteCloudJestConfiguration.ProjectType.ACP,
  }),
  testMatch: ["**/__tests__/**/*.test.js"],
  testEnvironment: "node",
  verbose: true,
};
