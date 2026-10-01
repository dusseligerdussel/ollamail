// openapi-typescript (`pnpm gen:api`) uses the TypeScript compiler API, which TypeScript 7
// (the project's compiler) no longer ships as JavaScript. Give it its own TypeScript 5
// instead of resolving the peer dependency to the project's version.
module.exports = {
  hooks: {
    readPackage(pkg) {
      if (pkg.name === "openapi-typescript") {
        delete pkg.peerDependencies?.typescript;
        pkg.dependencies = { ...pkg.dependencies, typescript: "^5.9.3" };
      }
      return pkg;
    },
  },
};
