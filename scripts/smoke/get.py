import sys, urllib.request
url=sys.argv[1]; data=sys.argv[2].encode() if len(sys.argv)>2 else None
req=urllib.request.Request(url,data=data,headers={"Content-Type":"application/json"} if data else {})
print(urllib.request.urlopen(req,timeout=90).read().decode())
