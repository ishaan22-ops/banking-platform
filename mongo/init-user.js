db.createUser({
  user: "harbor_transactions",
  pwd: process.env.MONGO_APP_PASSWORD,
  roles: [{ role: "readWrite", db: "harbor_transactions" }],
});
