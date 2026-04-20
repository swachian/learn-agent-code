#!/bin/bash
for i in {31..37}; do 
	echo -e "\033[$i;40mHello world\033[0m"
done

for i in {1..8}; do
	echo -e "\033[$i;31;40mHello world!\033[0m"
done
