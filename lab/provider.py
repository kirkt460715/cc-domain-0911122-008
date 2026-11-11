import datetime as dt,threading
from .http import ProtocolError,main
class Provider:
    def __init__(self):self.lock=threading.RLock();self.requests=[]
    def handle(self,method,path,args,body):
        with self.lock:
            if path=='/requests':return self.requests
            if path!='/readings':raise ProtocolError(404,'route')
            start=dt.datetime.fromisoformat(args['start'].replace('Z','+00:00'));end=dt.datetime.fromisoformat(args['end'].replace('Z','+00:00'))
            if start.tzinfo is None or end.tzinfo is None or start>=end:raise ProtocolError(400,'interval')
            item={'start':start.isoformat(),'end':end.isoformat(),'meter':args['meter']};self.requests.append(item)
            rows=[];point=start
            while point<end and len(rows)<100:
                rows.append({'id':'r'+str(int(point.timestamp())),'segment':args.get('segment','segment1'),'at':point.isoformat(),'value':int(point.timestamp())%100000000,'kind':'normal'})
                point+=dt.timedelta(hours=1)
            return {'rows':rows,'coverage':item if point>=end else None,'complete':point>=end}
if __name__=='__main__':main(Provider())
