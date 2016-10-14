/**
 * Copyright 2014 IBM Corp.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 **/

module.exports = function(RED) {
    "use strict";

    function AmazonCWLQueryNode(n) {
        RED.nodes.createNode(this,n);
        this.awsConfig = RED.nodes.getNode(n.aws);
        this.region = n.region;
        this.operation = n.operation;
        this.logGroupName = n.logGroupName;
        this.logStreamName = n.logStreamName;
        this.region = this.awsConfig.region;
        this.accessKey = this.awsConfig.accessKey;
        this.secretKey = this.awsConfig.secretKey;
        this.functionname = n.functionname;

        var node = this;
        var AWS = require("aws-sdk");
          AWS.config.update({
            accessKeyId: this.accessKey,
            secretAccessKey: this.secretKey,
            region: this.region
          });
        if (!AWS) {
            node.warn("Missing AWS credentials");
            return;
        }

        var cwl = new AWS.CloudWatchLogs( { 'region': node.region } );

        node.on("input", function(msg) {
            node.sendMsg = function (err, data) {
              if (err) {
                node.status({fill:"red",shape:"ring",text:"error"});
                node.error("failed: " + err.toString(),msg);
                node.warn("data: "+JSON.stringify(data.toString()));
                node.warn("msg: "+JSON.stringify(msg.toString()));

                return;
              } else {
                msg.payload = data;
                node.status({});
              }
              node.send(msg);
            };

            var logGroupName = msg.logGroupName || node.logGroupName;
            var logStreamName = msg.logStreamName || node.logStreamName;
            if (logStreamName === "" || logStreamName === "") {
                node.error("Log Group/Stream not specified",msg);
                return;
            }
            function cwl_get(cwl,msg,node){
              node.status({fill:"blue",shape:"dot",text:"getting"});
              var params = {
                logGroupName: logGroupName, /* required */
                logStreamName: logStreamName, /* required */
                startFromHead: msg.startFromHead || false

              };
              if (msg.nextToken){
                params.nextToken=msg.nextToken
              }
              //endTime: msg.endTime || 0,
//startTime: msg.startTime || 0
//limit: msg.limit ||1 ,

              cwl.getLogEvents(params, node.sendMsg);

            }

            function cwl_put(cwl,msg,node){
              node.status({fill:"blue",shape:"dot",text:"Putting"});
              var params = {
                logEvents: [ /* required */
                    {
                      message: msg.payload, /* required */
                      timestamp: msg.timestamp || Date.now() /* required */
                    },
                    /* more items */
                  ],
                  logGroupName: logGroupName, /* required */
                  logStreamName: logStreamName /* required */
              };

              if (msg.sequenceToken) {params.sequenceToken=msg.sequenceToken};
              console.log(JSON.stringify(params));
              cwl.putLogEvents(params, node.sendMsg);
            }

            switch (node.operation) {
              case 'get':
                cwl_get(cwl,msg,node);
                break;
              case 'put':
                cwl_put(cwl,msg,node);
                break;
              }
        });
    }
    RED.nodes.registerType("amazon cloudwatchlogs", AmazonCWLQueryNode);

};
