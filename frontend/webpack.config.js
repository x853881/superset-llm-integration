const path = require("path");
const { execSync } = require("child_process");
const { DefinePlugin } = require("webpack");
const { ModuleFederationPlugin } = require("webpack").container;
const packageConfig = require("./package");
const extensionConfig = require("../extension.json");

// The commit it's built from, and when, so the console can say which build is running.
function buildLabel() {
  const git = args => {
    try {
      return execSync(`git ${args}`, { cwd: __dirname, stdio: ["ignore", "pipe", "ignore"], timeout: 5000 }).toString().trim();
    } catch {
      return "";
    }
  };
  const commit = git("rev-parse --short HEAD") || "unknown commit";
  const changes = git("status --porcelain") ? " with local changes" : "";
  return `${commit}${changes}, built ${new Date().toISOString().slice(0, 16)}Z`;
}

module.exports = (env, argv) => {
  const isProd = argv.mode === "production";

  return {
    entry: isProd ? {} : "./src/index.tsx",
    mode: isProd ? "production" : "development",
    devServer: {
      port: 3000,
      headers: {
        "Access-Control-Allow-Origin": "*",
      },
    },
    output: {
      clean: true,
      filename: isProd ? undefined : "[name].[contenthash].js",
      chunkFilename: "[name].[contenthash].js",
      path: path.resolve(__dirname, "dist"),
      publicPath: `/api/v1/extensions/${extensionConfig.publisher}/${extensionConfig.name}/`,
    },
    resolve: {
      extensions: [".ts", ".tsx", ".js", ".jsx"],
    },
    externalsType: "window",
    externals: {
      "@apache-superset/core": "superset",
    },
    module: {
      rules: [
        {
          test: /\.tsx?$/,
          use: "ts-loader",
          exclude: /node_modules/,
        },
      ],
    },
    plugins: [
      // Build with NLSQL_IGNORE_ROLES=true to show the panel to everyone, whatever their roles.
      new DefinePlugin({
        __NLSQL_IGNORE_ROLES__: process.env.NLSQL_IGNORE_ROLES === "true",
        __NLSQL_BUILD__: JSON.stringify(buildLabel()),
      }),
      new ModuleFederationPlugin({
        name: "waxwing_nltosqlandback",
        filename: "remoteEntry.[contenthash].js",
        exposes: {
          "./index": "./src/index.tsx",
        },
        shared: {
          react: {
            singleton: true,
            requiredVersion: packageConfig.peerDependencies.react,
            import: false,
          },
          "react-dom": {
            singleton: true,
            requiredVersion: packageConfig.peerDependencies["react-dom"],
            import: false,
          },
          // Superset shares its own antd instance into the "default" share scope (eagerly, as a
          // singleton - see the host's webpack.config.js), so the panel's antd components are
          // literally the host's: same version, same ConfigProvider, therefore the same theme
          // tokens Superset's own UI is drawn with. `import: false` keeps antd out of this
          // bundle entirely rather than shipping a second copy that would have to be themed
          // separately.
          antd: {
            singleton: true,
            requiredVersion: packageConfig.peerDependencies.antd,
            import: false,
          },
        },
      }),
    ],
  };
};