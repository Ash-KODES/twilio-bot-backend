from firebase_admin import credentials, initialize_app, firestore, storage

# Firebase Initialization
cred = credentials.Certificate('./service.json')
print(cred)
app = initialize_app(cred, {"storageBucket": "your-bucket-name.appspot.com"})
db = firestore.client()
bucket = storage.bucket()
